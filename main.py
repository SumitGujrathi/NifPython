import os
import time
import io
import csv
import json
import re
import threading
import urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
import pyotp
from flask import Flask, render_template_string, request, Response, jsonify

app = Flask(__name__)

# --- ENVIRONMENT CONFIGURATION ---
CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID")
CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET")
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1:5000/")
USER_ID = os.getenv("UPSTOX_USER_ID")
PIN = os.getenv("UPSTOX_PIN")
TOTP_KEY = os.getenv("UPSTOX_TOTP_KEY")
ENV_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM")

SESSION_FILE_PATH = "/tmp/upstox_session.json"
AUTO_ORDER_STATE_PATH = "/tmp/upstox_auto_order_state.json"
DYNAMIC_TSL_STATE_PATH = "/tmp/upstox_dynamic_tsl_state.json"
AUTH_THREAD_LOCK = threading.Lock()
ORDER_EXECUTION_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()

IST = timezone(timedelta(hours=5, minutes=30))
DUPLICATE_COOLDOWN_SECONDS = 3600  # 1-hour gap required before re-entering same Symbol + CE/PE

INSTRUMENT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank"
}

OPEN_ORDER_STATUSES = {
    "open", "pending", "trigger pending", "open pending",
    "validation pending", "modify pending", "modify validation pending",
    "after market order req received", "put order req received",
    "scheduled", "active"
}

FAILED_ORDER_STATUSES = {
    "rejected", "cancelled", "canceled", "expired", "failed"
}

AUTH_COOLDOWN = {"last_failure_time": 0, "cooldown_duration": 60}

# --- PERSISTENT HTTP CONNECTION POOL ---
HTTP_SESSION = requests.Session()
_http_adapter = HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=0)
HTTP_SESSION.mount("https://", _http_adapter)
HTTP_SESSION.mount("http://", _http_adapter)

# --- IN-MEMORY CACHES & SINGLE-FLIGHT LOCKS ---
EXPIRY_CACHE = {}                 # symbol -> {"ts": float, "expiries": list}
CHAIN_CACHE = {}                  # (instrument_key, expiry) -> {"ts": float, "data": list}
CHAIN_REFRESH_INFLIGHT = set()

DASHBOARD_CFG_CACHE = {"ts": 0.0, "cfg": None, "latency_ms": 0}
DASHBOARD_REFRESH_INFLIGHT = False

HISTORY_CACHE = {}                # cache_key -> {"ts": float, "snapshots": list, "sheet_used": str}
HISTORY_INFLIGHT_LOCKS = {}       # cache_key -> threading.Lock()
ACTIVE_ANALYSIS_SYMBOLS = {"NIFTY": time.time()}

LAST_DIAGNOSTICS = {
    "upstox_ms": 0,
    "gsheet_ms": 0,
    "total_cycle_ms": 0,
    "gsheet_status": "OK",
    "upstox_status": "OK",
    "updated_at": "-"
}

def get_upstox_proxies():
    proxy_url = (
        os.getenv("STATIC_IP_PROXY_URL")
        or os.getenv("QUOTAGUARDSHIELD_URL")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
    )
    if proxy_url and proxy_url.strip():
        clean = proxy_url.strip()
        return {"http": clean, "https": clean}
    return None

# --- SESSION MANAGEMENT ---
def load_session_token():
    try:
        if os.path.exists(SESSION_FILE_PATH):
            with open(SESSION_FILE_PATH, "r") as f:
                data = json.load(f)
                if time.time() - data.get("generated_at", 0) < 43200:
                    return data.get("access_token")
    except Exception:
        pass
    if ENV_ACCESS_TOKEN and ENV_ACCESS_TOKEN.strip():
        return ENV_ACCESS_TOKEN.strip()
    return None

def save_session_token(token):
    try:
        with open(SESSION_FILE_PATH, "w") as f:
            json.dump({"access_token": token, "generated_at": time.time()}, f)
    except Exception:
        pass

def invalidate_session():
    if os.path.exists(SESSION_FILE_PATH):
        try:
            os.remove(SESSION_FILE_PATH)
        except Exception:
            pass

def perform_single_auth(force_refresh=False):
    with AUTH_THREAD_LOCK:
        if not force_refresh:
            existing = load_session_token()
            if existing:
                return existing
        now = time.time()
        if now - AUTH_COOLDOWN["last_failure_time"] < AUTH_COOLDOWN["cooldown_duration"]:
            return None
        if not all([CLIENT_ID, USER_ID, PIN, TOTP_KEY]):
            return ENV_ACCESS_TOKEN.strip() if (ENV_ACCESS_TOKEN and ENV_ACCESS_TOKEN.strip()) else None
        try:
            clean_totp = str(TOTP_KEY).strip().upper().replace(" ", "").replace("-", "")
            totp_code = str(pyotp.TOTP(clean_totp).now()).zfill(6)
            res = HTTP_SESSION.post(
                "https://api.upstox.com/v2/totp-login/authorization/token",
                json={"totp": totp_code, "pin": str(PIN).strip()},
                headers={"accept": "application/json", "Content-Type": "application/json", "x-api-key": str(CLIENT_ID).strip()},
                proxies=get_upstox_proxies(),
                timeout=(2.5, 5.0)
            )
            if res.status_code == 200:
                jd = res.json()
                token = jd.get("access_token") or jd.get("data", {}).get("access_token")
                if token:
                    save_session_token(token)
                    AUTH_COOLDOWN["last_failure_time"] = 0
                    return token
            AUTH_COOLDOWN["last_failure_time"] = time.time()
        except Exception:
            AUTH_COOLDOWN["last_failure_time"] = time.time()
        return None

def get_valid_token():
    return load_session_token() or perform_single_auth()

# --- STATIC IP CHECK & REGISTRATION ---
def get_current_outbound_ip():
    proxies = get_upstox_proxies()
    for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/ip"):
        try:
            res = HTTP_SESSION.get(url, proxies=proxies, timeout=(2.0, 3.5))
            if res.status_code == 200:
                return res.json().get("ip", "").strip() if "ipify" in url else res.text.strip()
        except Exception:
            continue
    return ""

def fetch_upstox_static_ip_status():
    outbound_ip = get_current_outbound_ip()
    token = get_valid_token()
    if not token:
        return {"outbound_ip": outbound_ip, "primary_ip": None, "secondary_ip": None, "is_matched": False, "error": "Upstox access token unavailable."}
    try:
        res = HTTP_SESSION.get(
            "https://api.upstox.com/v2/user/ip",
            headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            proxies=get_upstox_proxies(),
            timeout=(2.5, 4.5)
        )
        if res.status_code == 200:
            data = res.json().get("data", {}) or {}
            p_ip, s_ip = data.get("primary_ip") or "", data.get("secondary_ip") or ""
            return {"outbound_ip": outbound_ip, "primary_ip": p_ip, "secondary_ip": s_ip, "is_matched": bool(outbound_ip and outbound_ip in (p_ip, s_ip)), "error": None}
        rj = res.json() if res.content else {}
        err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
        return {"outbound_ip": outbound_ip, "primary_ip": None, "secondary_ip": None, "is_matched": False, "error": f"HTTP {res.status_code}: {err}"}
    except Exception as e:
        return {"outbound_ip": outbound_ip, "primary_ip": None, "secondary_ip": None, "is_matched": False, "error": str(e)}

def update_upstox_static_ip(primary_ip, secondary_ip=None):
    token = get_valid_token()
    if not token:
        return False, "Upstox access token unavailable."
    clean_p = str(primary_ip or "").strip()
    if not clean_p:
        return False, "Primary IP is required."
    payload = {"primary_ip": clean_p}
    clean_s = str(secondary_ip or "").strip()
    if clean_s and clean_s != clean_p:
        payload["secondary_ip"] = clean_s
    try:
        res = HTTP_SESSION.put(
            "https://api.upstox.com/v2/user/ip",
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            proxies=get_upstox_proxies(),
            timeout=(2.5, 5.0)
        )
        rj = res.json() if res.content else {}
        if res.status_code == 200 and rj.get("status") == "success":
            invalidate_session()
            new_tok = perform_single_auth(force_refresh=True)
            note = "Session refreshed via TOTP." if new_tok else "Please re-authenticate."
            return True, f"Static IP Registered ({clean_p})! {note}"
        err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
        return False, f"Static IP Update Rejected ({res.status_code}): {err}"
    except Exception as e:
        return False, f"Static IP Update Exception: {e}"

# --- TIME & PERSISTENT STATE HELPERS ---
def get_ist_now():
    return datetime.now(IST)

def get_ist_today_str():
    return get_ist_now().strftime("%Y-%m-%d")

def parse_upstox_order_timestamp(ts_str):
    if not ts_str:
        return 0.0
    s = str(ts_str).strip()
    try:
        iso_s = s.replace("Z", "+00:00")
        if "T" in iso_s:
            dt = datetime.fromisoformat(iso_s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.timestamp()
    except Exception:
        pass
    s_clean = re.sub(r'\.\d+$', '', s)
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S", "%d-%b-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%b-%Y %H:%M"
    ):
        try:
            return datetime.strptime(s_clean, fmt).replace(tzinfo=IST).timestamp()
        except ValueError:
            pass
    return 0.0

def format_duration_hm(seconds):
    sec = max(0, int(seconds))
    mins, rem_s = divmod(sec, 60)
    hrs, rem_m = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}h {rem_m}m"
    return f"{rem_m}m {rem_s}s"

def load_auto_order_state():
    today = get_ist_today_str()
    try:
        if os.path.exists(AUTO_ORDER_STATE_PATH):
            with open(AUTO_ORDER_STATE_PATH, "r") as f:
                data = json.load(f)
                if data.get("date") == today:
                    return data
    except Exception:
        pass
    return {"date": today, "recent_submissions": {}, "cancelled_signatures": [], "exit_cooldowns": {}}

def save_auto_order_state(state):
    try:
        with open(AUTO_ORDER_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

def load_dynamic_tsl_state():
    today = get_ist_today_str()
    try:
        if os.path.exists(DYNAMIC_TSL_STATE_PATH):
            with open(DYNAMIC_TSL_STATE_PATH, "r") as f:
                data = json.load(f)
                if data.get("date") == today:
                    return data
    except Exception:
        pass
    return {"date": today, "contracts": {}, "by_token": {}}

def save_dynamic_tsl_state(state):
    try:
        with open(DYNAMIC_TSL_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

def round_tick(price, tick=0.05):
    if price is None or price <= 0:
        return 0.05
    return round(round(price / tick) * tick, 2)

# --- DYNAMIC TRAILING SL & TARGET ENGINE ---
def compute_dynamic_sl_target(side, entry_price, current_ltp, prev_best_price, sl_percent, target_percent, prev_sl=None, prev_target=None):
    side = str(side).upper()
    entry_price = float(entry_price)
    current_ltp = float(current_ltp) if current_ltp and current_ltp > 0 else entry_price

    if side == "BUY":
        if prev_best_price is None or prev_best_price <= 0:
            prev_best_price = entry_price
        best_price = max(entry_price, prev_best_price, current_ltp)
        fav = max(0.0, best_price - entry_price)

        new_sl = None
        if sl_percent is not None and sl_percent > 0:
            cand = round_tick(entry_price * (1.0 - sl_percent / 100.0) + fav * (sl_percent / 100.0))
            new_sl = max(float(prev_sl), cand) if prev_sl is not None else cand

        new_target = None
        if target_percent is not None and target_percent > 0:
            cand = round_tick(entry_price * (1.0 + target_percent / 100.0) + fav * (target_percent / 100.0))
            new_target = max(float(prev_target), cand) if prev_target is not None else cand
    else:
        if prev_best_price is None or prev_best_price <= 0:
            prev_best_price = entry_price
        best_price = min(entry_price, prev_best_price, current_ltp)
        fav = max(0.0, entry_price - best_price)

        new_sl = None
        if sl_percent is not None and sl_percent > 0:
            cand = round_tick(entry_price * (1.0 + sl_percent / 100.0) - fav * (sl_percent / 100.0))
            new_sl = min(float(prev_sl), cand) if prev_sl is not None else cand

        new_target = None
        if target_percent is not None and target_percent > 0:
            cand = round_tick(entry_price * (1.0 - target_percent / 100.0) - fav * (target_percent / 100.0))
            new_target = min(float(prev_target), cand) if prev_target is not None else cand

    return {"best_price": round(best_price, 2), "favorable_move": round(fav, 2), "new_sl": new_sl, "new_target": new_target}

def modify_upstox_gtt_sl_target(gtt_order, new_sl, new_target, new_entry=None, new_qty=None):
    token = get_valid_token()
    if not token:
        return False, "Upstox token unavailable."
    gtt_order_id = gtt_order.get("gtt_order_id")
    raw_rules = gtt_order.get("raw_rules") or []
    entry_rule = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "ENTRY"), None)
    if not gtt_order_id or not entry_rule:
        return False, "Missing GTT ID or ENTRY rule."

    entry_trigger = float(new_entry) if (new_entry is not None and float(new_entry) > 0) else float(entry_rule.get("trigger_price", 0.05))
    qty_val = int(new_qty) if (new_qty is not None and int(new_qty) > 0) else int(gtt_order.get("quantity", 1))

    updated_rules = [{"strategy": "ENTRY", "trigger_type": entry_rule.get("trigger_type", "IMMEDIATE"), "trigger_price": entry_trigger}]
    existing_tgt = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "TARGET"), None)
    if new_target is not None and float(new_target) > 0:
        updated_rules.append({"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": float(new_target)})
    elif existing_tgt and new_target is None:
        updated_rules.append({"strategy": "TARGET", "trigger_type": existing_tgt.get("trigger_type", "IMMEDIATE"), "trigger_price": float(existing_tgt.get("trigger_price", 0.05))})

    existing_sl = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "STOPLOSS"), None)
    if new_sl is not None and float(new_sl) > 0:
        updated_rules.append({"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": float(new_sl)})
    elif existing_sl and new_sl is None:
        updated_rules.append({"strategy": "STOPLOSS", "trigger_type": existing_sl.get("trigger_type", "IMMEDIATE"), "trigger_price": float(existing_sl.get("trigger_price", 0.05))})

    try:
        res = HTTP_SESSION.put(
            "https://api.upstox.com/v3/order/gtt/modify",
            json={"type": gtt_order.get("type", "MULTIPLE"), "quantity": qty_val, "gtt_order_id": gtt_order_id, "rules": updated_rules},
            headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            proxies=get_upstox_proxies(),
            timeout=(2.5, 5.0)
        )
        rj = res.json() if res.content else {}
        if res.status_code == 200 and rj.get("status") == "success":
            return True, f"Updated GTT {gtt_order_id} -> Qty: {qty_val} | Entry: {entry_trigger} | SL: {new_sl or 'N/A'} | Target: {new_target or 'N/A'}"
        err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
        return False, f"GTT Modify Rejected ({res.status_code}): {err}"
    except Exception as e:
        return False, f"GTT Modify Exception: {e}"

def modify_upstox_regular_order(order_id, quantity, price, trigger_price=0.0, order_type="LIMIT", validity="DAY", disclosed_quantity=0):
    token = get_valid_token()
    if not token:
        return False, "Upstox token unavailable."
    clean_oid = str(order_id or "").strip()
    if not clean_oid:
        return False, "Order ID is required."

    clean_type = str(order_type or "LIMIT").strip().upper()
    if clean_type not in ("LIMIT", "MARKET", "SL", "SL-M"):
        clean_type = "LIMIT"

    qty_val = int(quantity)
    if qty_val <= 0:
        return False, "Quantity must be greater than 0."

    price_val = 0.0 if clean_type in ("MARKET", "SL-M") else round_tick(float(price or 0))
    trig_val = round_tick(float(trigger_price or 0)) if clean_type in ("SL", "SL-M") else 0.0

    if clean_type in ("LIMIT", "SL") and price_val <= 0:
        return False, f"Limit price must be > 0 for {clean_type} orders."
    if clean_type in ("SL", "SL-M") and trig_val <= 0:
        return False, f"Trigger price must be > 0 for {clean_type} orders."

    payload = {
        "quantity": qty_val,
        "validity": str(validity or "DAY").strip().upper() or "DAY",
        "price": price_val,
        "order_id": clean_oid,
        "order_type": clean_type,
        "disclosed_quantity": int(disclosed_quantity or 0),
        "trigger_price": trig_val
    }
    try:
        res = HTTP_SESSION.put(
            "https://api-hft.upstox.com/v3/order/modify",
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            proxies=get_upstox_proxies(),
            timeout=(2.5, 5.0)
        )
        rj = res.json() if res.content else {}
        if res.status_code == 200 and rj.get("status") == "success":
            return True, f"Order {clean_oid} Modified -> {clean_type} | Qty: {qty_val} | Price: ₹{price_val} | Trigger: ₹{trig_val}"
        err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
        return False, f"Order Modify Rejected ({res.status_code}): {err}"
    except Exception as e:
        return False, f"Order Modify Exception: {e}"

def process_dynamic_sl_and_target(positions, gtt_orders, default_sl_pct=None, default_target_pct=None):
    tsl_state = load_dynamic_tsl_state()
    contracts_map = tsl_state.setdefault("contracts", {})
    by_token_map = tsl_state.setdefault("by_token", {})
    state_dirty = False
    updates_log = []

    active_gtt_by_symbol = {}
    for g in gtt_orders:
        ts = str(g.get("trading_symbol", "")).strip().upper()
        if ts and str(g.get("status", "")).strip().lower() in OPEN_ORDER_STATUSES:
            active_gtt_by_symbol[ts] = g

    for p in positions:
        qty = p.get("quantity", 0) or 0
        ts = str(p.get("trading_symbol", "")).strip().upper()
        inst_token = str(p.get("instrument_token", "")).strip()
        ltp = float(p.get("ltp", 0.0) or 0.0)
        p["best_price"] = None
        p["dynamic_sl"] = None
        p["dynamic_target"] = None

        if qty == 0 or not ts:
            if ts in contracts_map:
                del contracts_map[ts]
                state_dirty = True
            continue

        side = "BUY" if qty > 0 else "SELL"
        entry_price = float(p.get("buy_price", 0.0) if side == "BUY" else p.get("sell_price", 0.0)) or ltp
        matching_gtt = active_gtt_by_symbol.get(ts)
        rec = contracts_map.get(ts)
        if not rec and inst_token and inst_token in by_token_map:
            rec = dict(by_token_map[inst_token])
            contracts_map[ts] = rec
            state_dirty = True

        if not rec:
            gtt_entry, gtt_sl, gtt_tgt = entry_price, None, None
            inf_sl, inf_tgt = default_sl_pct, default_target_pct
            if matching_gtt:
                raw_rules = matching_gtt.get("raw_rules") or []
                er = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "ENTRY"), None)
                sr = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "STOPLOSS"), None)
                tr = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "TARGET"), None)
                if er and float(er.get("trigger_price", 0)) > 0:
                    gtt_entry = float(er["trigger_price"])
                if sr and float(sr.get("trigger_price", 0)) > 0:
                    gtt_sl = float(sr["trigger_price"])
                    if gtt_entry > 0:
                        inf_sl = round(abs(gtt_entry - gtt_sl) / gtt_entry * 100.0, 4)
                if tr and float(tr.get("trigger_price", 0)) > 0:
                    gtt_tgt = float(tr["trigger_price"])
                    if gtt_entry > 0:
                        inf_tgt = round(abs(gtt_tgt - gtt_entry) / gtt_entry * 100.0, 4)
            if (inf_sl is None or inf_sl <= 0) and (inf_tgt is None or inf_tgt <= 0):
                continue
            rec = {
                "side": side,
                "entry_price": entry_price if entry_price > 0 else gtt_entry,
                "best_price": entry_price if entry_price > 0 else gtt_entry,
                "sl_percent": inf_sl,
                "target_percent": inf_tgt,
                "current_sl": gtt_sl,
                "current_target": gtt_tgt
            }
            contracts_map[ts] = rec
            state_dirty = True

        if entry_price > 0 and abs(rec.get("entry_price", 0) - entry_price) > 0.01:
            rec["entry_price"] = entry_price
            state_dirty = True

        sl_pct, tgt_pct = rec.get("sl_percent"), rec.get("target_percent")
        if (sl_pct is None or sl_pct <= 0) and (tgt_pct is None or tgt_pct <= 0):
            continue

        broker_sl, broker_tgt = rec.get("current_sl"), rec.get("current_target")
        if matching_gtt:
            raw_rules = matching_gtt.get("raw_rules") or []
            sr = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "STOPLOSS"), None)
            tr = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "TARGET"), None)
            if sr and float(sr.get("trigger_price", 0)) > 0:
                g_sl = float(sr["trigger_price"])
                broker_sl = max(broker_sl, g_sl) if (side == "BUY" and broker_sl is not None) else (min(broker_sl, g_sl) if (side == "SELL" and broker_sl is not None) else g_sl)
            if tr and float(tr.get("trigger_price", 0)) > 0:
                g_tgt = float(tr["trigger_price"])
                broker_tgt = max(broker_tgt, g_tgt) if (side == "BUY" and broker_tgt is not None) else (min(broker_tgt, g_tgt) if (side == "SELL" and broker_tgt is not None) else g_tgt)

        dyn = compute_dynamic_sl_target(side, rec["entry_price"], ltp, rec.get("best_price"), sl_pct, tgt_pct, broker_sl, broker_tgt)
        if dyn["best_price"] != rec.get("best_price"):
            rec["best_price"] = dyn["best_price"]
            state_dirty = True

        new_sl, new_target = dyn["new_sl"], dyn["new_target"]
        sl_imp = new_sl is not None and (broker_sl is None or (side == "BUY" and new_sl - broker_sl >= 0.05) or (side == "SELL" and broker_sl - new_sl >= 0.05))
        tgt_imp = new_target is not None and (broker_tgt is None or (side == "BUY" and new_target - broker_tgt >= 0.05) or (side == "SELL" and broker_tgt - new_target >= 0.05))

        if sl_imp and ltp > 0 and ((side == "BUY" and new_sl >= ltp) or (side == "SELL" and new_sl <= ltp)):
            sl_imp = False

        if (sl_imp or tgt_imp) and matching_gtt:
            ok, mod_msg = modify_upstox_gtt_sl_target(matching_gtt, new_sl if sl_imp else broker_sl, new_target if tgt_imp else broker_tgt)
            if ok:
                rec["current_sl"] = new_sl if sl_imp else broker_sl
                rec["current_target"] = new_target if tgt_imp else broker_tgt
                state_dirty = True
                updates_log.append(f"Dynamic Trail ({ts}): {mod_msg}")
        else:
            if new_sl is not None and rec.get("current_sl") != new_sl:
                rec["current_sl"] = new_sl
                state_dirty = True
            if new_target is not None and rec.get("current_target") != new_target:
                rec["current_target"] = new_target
                state_dirty = True

        p["best_price"] = rec.get("best_price")
        p["dynamic_sl"] = rec.get("current_sl")
        p["dynamic_target"] = rec.get("current_target")

    if state_dirty:
        save_dynamic_tsl_state(tsl_state)
    return updates_log

# --- EXIT CONDITION (B7 -> B29 BE/SE) & 3:20 PM AUTO EXIT (B8 == YES) ---
def cancel_upstox_regular_order(order_id, headers, proxies):
    try:
        res = HTTP_SESSION.delete(
            f"https://api-hft.upstox.com/v3/order/cancel?order_id={urllib.parse.quote(str(order_id))}",
            headers=headers,
            proxies=proxies,
            timeout=(2.5, 4.5)
        )
        rj = res.json() if res.content else {}
        if res.status_code == 200 and rj.get("status") == "success":
            return True, f"Order {order_id} cancelled"
        err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
        return False, f"Order {order_id} cancel failed: {err}"
    except Exception as e:
        return False, f"Order {order_id} cancel error: {e}"

def cancel_upstox_gtt_order(gtt_order_id, headers, proxies):
    try:
        res = HTTP_SESSION.delete(
            "https://api.upstox.com/v3/order/gtt/cancel",
            json={"gtt_order_id": str(gtt_order_id)},
            headers=headers,
            proxies=proxies,
            timeout=(2.5, 4.5)
        )
        rj = res.json() if res.content else {}
        if res.status_code == 200 and rj.get("status") == "success":
            return True, f"GTT {gtt_order_id} cancelled"
        err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
        return False, f"GTT {gtt_order_id} cancel failed: {err}"
    except Exception as e:
        return False, f"GTT {gtt_order_id} cancel error: {e}"

def exit_position_at_market(pos, headers, proxies):
    qty = int(pos.get("quantity", 0) or 0)
    if qty == 0:
        return True, "Already flat"
    inst_token = pos.get("instrument_token", "")
    ts = pos.get("trading_symbol", "")
    if not inst_token:
        return False, f"Missing instrument_token for {ts}"

    raw_prod = str(pos.get("product", "I")).strip().upper()
    product = "I" if raw_prod in ("I", "MIS", "INTRADAY") else "D"
    exit_txn = "SELL" if qty > 0 else "BUY"

    payload = {
        "quantity": abs(qty),
        "product": product,
        "validity": "DAY",
        "price": 0,
        "tag": "DASHBOARD_EXIT",
        "instrument_token": inst_token,
        "order_type": "MARKET",
        "transaction_type": exit_txn,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False
    }
    try:
        res = HTTP_SESSION.post(
            "https://api-hft.upstox.com/v3/order/place",
            json=payload,
            headers=headers,
            proxies=proxies,
            timeout=(2.5, 5.0)
        )
        rj = res.json() if res.content else {}
        if res.status_code == 200 and rj.get("status") == "success":
            oids = rj.get("data", {}).get("order_ids", [])
            oid = oids[0] if oids else rj.get("data", {}).get("order_id", "OK")
            return True, f"Exited {ts} ({exit_txn} {abs(qty)} @ MARKET, ID: {oid})"
        err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
        return False, f"Market Exit Rejected for {ts} ({res.status_code}): {err}"
    except Exception as e:
        return False, f"Market Exit Exception for {ts}: {e}"

def execute_targeted_or_full_exit(positions, orders, gtt_orders, target_underlying=None, target_opt_type=None, only_options=False):
    token = get_valid_token()
    if not token:
        return False, "Upstox token unavailable for exit."

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    proxies = get_upstox_proxies()

    def matches_contract(trading_symbol):
        if target_underlying is None and target_opt_type is None and not only_options:
            return True
        u, opt = extract_symbol_and_opt_type(trading_symbol)
        if target_underlying is not None and u != target_underlying.upper():
            return False
        if target_opt_type is not None and opt != target_opt_type.upper():
            return False
        if only_options and opt not in ("CE", "PE"):
            return False
        return True

    matching_positions = [
        p for p in positions
        if (p.get("quantity", 0) or 0) != 0 and matches_contract(p.get("trading_symbol", ""))
    ]
    matching_open_orders = [
        o for o in orders
        if str(o.get("status", "")).strip().lower() in OPEN_ORDER_STATUSES
        and o.get("order_id")
        and matches_contract(o.get("trading_symbol", ""))
    ]
    matching_gtt_orders = [
        g for g in gtt_orders
        if str(g.get("status", "")).strip().lower() in OPEN_ORDER_STATUSES
        and g.get("gtt_order_id")
        and matches_contract(g.get("trading_symbol", ""))
    ]

    if not matching_positions and not matching_open_orders and not matching_gtt_orders:
        return None, None

    logs = []
    all_ok = True

    cancel_tasks = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for o in matching_open_orders:
            cancel_tasks.append(ex.submit(cancel_upstox_regular_order, o["order_id"], headers, proxies))
        for g in matching_gtt_orders:
            cancel_tasks.append(ex.submit(cancel_upstox_gtt_order, g["gtt_order_id"], headers, proxies))
        for fut in as_completed(cancel_tasks):
            ok, msg = fut.result()
            logs.append(msg)
            if not ok:
                all_ok = False

    if matching_positions:
        with ThreadPoolExecutor(max_workers=4) as ex:
            exit_futs = [ex.submit(exit_position_at_market, p, headers, proxies) for p in matching_positions]
            for fut in as_completed(exit_futs):
                ok, msg = fut.result()
                logs.append(msg)
                if not ok:
                    all_ok = False

    return all_ok, " | ".join(logs)

# --- FAST STALE-WHILE-REVALIDATE GOOGLE SHEET DASHBOARD CONFIG (B7, B8, B16:B29) ---
def _download_dashboard_config():
    t0 = time.time()
    sheet_id = SPREADSHEET_ID or "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM"
    cb = int(time.time() * 1000)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&headers=0&sheet=DASHBOARD&range=A1:D35&_cb={cb}"

    raw_csv, last_err = None, None
    try:
        res = HTTP_SESSION.get(url, headers=headers, timeout=(2.0, 3.0))
        if res.status_code == 200:
            text = res.text.lstrip('\ufeff')
            if text.strip().startswith("<!DOCTYPE") or "<html" in text.lower():
                last_err = "Google Sheet is private. Set sharing to 'Anyone with the link can view'."
            elif len(text.strip()) > 5:
                raw_csv = text
        else:
            last_err = f"HTTP {res.status_code} fetching DASHBOARD sheet."
    except Exception as e:
        last_err = f"Google Sheet slow/timeout: {e}"

    latency_ms = int((time.time() - t0) * 1000)
    LAST_DIAGNOSTICS["gsheet_ms"] = latency_ms

    if not raw_csv:
        LAST_DIAGNOSTICS["gsheet_status"] = f"WARN ({last_err})"
        return None, latency_ms, last_err or "Could not fetch DASHBOARD sheet."

    try:
        grid = list(csv.reader(io.StringIO(raw_csv.lstrip('\ufeff'))))

        label_values = {}
        for row in grid:
            for c_idx, cell in enumerate(row):
                norm_key = re.sub(r'[^a-z0-9]+', '_', str(cell).strip().lower()).strip('_')
                if norm_key and c_idx + 1 < len(row):
                    val = str(row[c_idx + 1]).strip()
                    if norm_key not in label_values or val != "":
                        label_values[norm_key] = val

        def get_b_cell(row_num_1based):
            idx = row_num_1based - 1
            if 0 <= idx < len(grid) and len(grid[idx]) >= 2:
                return str(grid[idx][1]).strip()
            return ""

        def resolve_field(keys, row_num_1based, prefer_exact_row=False):
            exact_val = get_b_cell(row_num_1based)
            if prefer_exact_row and exact_val != "":
                return exact_val
            for k in keys:
                if k in label_values and label_values[k] != "":
                    return label_values[k]
            return exact_val

        b7_raw = resolve_field(
            ["signal_exit", "be_se_exit", "exit_on_signal", "auto_exit_signal", "exit_signal_enabled"],
            7,
            prefer_exact_row=True
        ).strip().upper()
        signal_exit_b7 = "YES" if b7_raw in ("YES", "Y", "TRUE", "1") else "NO"

        b8_raw = resolve_field(
            ["3_20_pm_exit", "auto_exit_3_20", "exit_at_3_20", "square_off_3_20", "auto_square_off", "3_20_exit", "320_pm_exit"],
            8,
            prefer_exact_row=True
        ).strip().upper()
        auto_exit_320_b8 = "YES" if b8_raw in ("YES", "Y", "TRUE", "1") else "NO"

        manual_raw = resolve_field(["manual_order"], 16)
        manual_mode = "Auto" if manual_raw.strip().lower() == "auto" else "Manual"

        want_raw = resolve_field(["want_to_trade"], 17).strip().upper()
        want_to_trade = "YES" if want_raw in ("YES", "Y", "TRUE", "1") else "NO"

        amt_raw = resolve_field(["amount_to_be_invest", "amount_to_invest"], 18).replace(",", "").strip()
        amount_to_invest = float(amt_raw) if amt_raw else 0.0

        sym_raw = resolve_field(["symbol"], 19).strip().upper()
        symbol = "BANKNIFTY" if "BANK" in sym_raw else "NIFTY"

        expiry_month = resolve_field(["expiry_month"], 20).strip().upper()

        strike_raw = resolve_field(["strike_price"], 21).replace(",", "").strip()
        strike_price = int(float(strike_raw)) if strike_raw else 0

        opt_raw = resolve_field(["option_type"], 22).strip().upper()
        option_type = opt_raw if opt_raw in ("CE", "PE") else "CE"

        txn_raw = resolve_field(["transaction_type"], 23).strip().upper()
        transaction_type = txn_raw if txn_raw in ("BUY", "SELL") else "BUY"

        qty_raw = resolve_field(["quantity"], 24).replace(",", "").strip()
        quantity = int(float(qty_raw)) if qty_raw else 0

        prod_raw = resolve_field(["product"], 25).strip().upper()
        product = "I" if prod_raw in ("I", "INTRADAY", "MIS") else "D"

        sl_raw = resolve_field(["sl_percent"], 26).replace("%", "").strip()
        sl_percent = float(sl_raw) if sl_raw != "" else None

        tgt_raw = resolve_field(["target_percent"], 27).replace("%", "").strip()
        target_percent = float(tgt_raw) if tgt_raw != "" else None

        active_raw = resolve_field(["active_trade", "active_trades", "max_active_trade", "max_active_trades"], 28).replace(",", "").strip()
        max_active_trades = int(float(active_raw)) if active_raw != "" else 1

        exit_cond_raw = resolve_field(
            ["exit_condition", "exit_signal", "exit", "exit_cond", "ce_pe_exit", "trade_exit", "exit_type", "exit_status"],
            29,
            prefer_exact_row=True
        ).strip().upper()
        if exit_cond_raw not in ("BE", "SE"):
            exit_cond_raw = ""

        cfg = {
            "signal_exit_b7": signal_exit_b7,
            "auto_exit_320_b8": auto_exit_320_b8,
            "manual_order": manual_mode,
            "manual_order_raw": manual_raw or manual_mode,
            "want_to_trade": want_to_trade,
            "amount_to_invest": amount_to_invest,
            "symbol": symbol,
            "expiry_month": expiry_month,
            "strike_price": strike_price,
            "option_type": option_type,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": product,
            "sl_percent": sl_percent,
            "target_percent": target_percent,
            "max_active_trades": max_active_trades,
            "exit_condition": exit_cond_raw
        }
        LAST_DIAGNOSTICS["gsheet_status"] = "OK"
        return cfg, latency_ms, None
    except Exception as e:
        LAST_DIAGNOSTICS["gsheet_status"] = f"ERR ({e})"
        return None, latency_ms, f"Error parsing DASHBOARD sheet: {e}"

def _bg_refresh_dashboard_config():
    global DASHBOARD_REFRESH_INFLIGHT
    try:
        cfg, latency_ms, err = _download_dashboard_config()
        if not err and cfg is not None:
            with CACHE_LOCK:
                DASHBOARD_CFG_CACHE["ts"] = time.time()
                DASHBOARD_CFG_CACHE["cfg"] = cfg
                DASHBOARD_CFG_CACHE["latency_ms"] = latency_ms
    finally:
        with CACHE_LOCK:
            DASHBOARD_REFRESH_INFLIGHT = False

def fetch_dashboard_order_config(force_fresh=False):
    global DASHBOARD_REFRESH_INFLIGHT
    now = time.time()
    trigger_bg = False

    with CACHE_LOCK:
        cached_cfg = DASHBOARD_CFG_CACHE["cfg"]
        age = now - DASHBOARD_CFG_CACHE["ts"]
        if not force_fresh and cached_cfg is not None:
            if age < 2.0:
                return dict(cached_cfg), None
            if age < 60.0:
                if not DASHBOARD_REFRESH_INFLIGHT:
                    DASHBOARD_REFRESH_INFLIGHT = True
                    trigger_bg = True
                return dict(cached_cfg), None

    if trigger_bg:
        threading.Thread(target=_bg_refresh_dashboard_config, daemon=True).start()

    cfg, latency_ms, err = _download_dashboard_config()
    if not err and cfg is not None:
        with CACHE_LOCK:
            DASHBOARD_CFG_CACHE["ts"] = time.time()
            DASHBOARD_CFG_CACHE["cfg"] = cfg
            DASHBOARD_CFG_CACHE["latency_ms"] = latency_ms
        return dict(cfg), None

    with CACHE_LOCK:
        if DASHBOARD_CFG_CACHE["cfg"] is not None:
            return dict(DASHBOARD_CFG_CACHE["cfg"]), None
    return None, err or "Could not fetch DASHBOARD sheet."

def get_cached_expiries(symbol, headers):
    now = time.time()
    with CACHE_LOCK:
        entry = EXPIRY_CACHE.get(symbol)
        if entry and (now - entry["ts"]) < 3600 and entry["expiries"]:
            return entry["expiries"], None

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    try:
        res = HTTP_SESSION.get(
            f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}",
            headers=headers,
            timeout=(2.5, 4.0)
        )
        if res.status_code == 401:
            invalidate_session()
            new_tok = perform_single_auth(force_refresh=True)
            if new_tok:
                headers["Authorization"] = f"Bearer {new_tok}"
                res = HTTP_SESSION.get(
                    f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}",
                    headers=headers,
                    timeout=(2.5, 4.0)
                )
        if res.status_code != 200:
            with CACHE_LOCK:
                if symbol in EXPIRY_CACHE and EXPIRY_CACHE[symbol]["expiries"]:
                    return EXPIRY_CACHE[symbol]["expiries"], None
            return [], f"Expiry lookup failed: {res.text}"

        expiries = sorted(list(set(item["expiry"] for item in res.json().get("data", []))))
        if expiries:
            with CACHE_LOCK:
                EXPIRY_CACHE[symbol] = {"ts": now, "expiries": expiries}
        return expiries, None
    except Exception as e:
        with CACHE_LOCK:
            if symbol in EXPIRY_CACHE and EXPIRY_CACHE[symbol]["expiries"]:
                return EXPIRY_CACHE[symbol]["expiries"], None
        return [], f"Expiry lookup error: {e}"

def resolve_monthly_expiry_date(symbol, expiry_month, headers):
    expiries, err = get_cached_expiries(symbol, headers)
    if err or not expiries:
        return None, err or f"No option expiries found for {symbol}."

    month_lookup = {
        "JAN": 1, "JANUARY": 1, "01": 1, "1": 1, "FEB": 2, "FEBRUARY": 2, "02": 2, "2": 2,
        "MAR": 3, "MARCH": 3, "03": 3, "3": 3, "APR": 4, "APRIL": 4, "04": 4, "4": 4,
        "MAY": 5, "05": 5, "5": 5, "JUN": 6, "JUNE": 6, "06": 6, "6": 6,
        "JUL": 7, "JULY": 7, "07": 7, "7": 7, "AUG": 8, "AUGUST": 8, "08": 8, "8": 8,
        "SEP": 9, "SEPT": 9, "SEPTEMBER": 9, "09": 9, "9": 9, "OCT": 10, "OCTOBER": 10, "10": 10,
        "NOV": 11, "NOVEMBER": 11, "11": 11, "DEC": 12, "DECEMBER": 12, "12": 12
    }

    today_str = get_ist_today_str()
    future_expiries = [e for e in expiries if e >= today_str] or expiries
    target_m = month_lookup.get(str(expiry_month).strip().upper())
    if not target_m:
        return (expiry_month, None) if expiry_month in expiries else (future_expiries[0], None)

    matching = [e for e in future_expiries if int(e.split("-")[1]) == target_m]
    if matching:
        first_year = sorted(list(set(e.split("-")[0] for e in matching)))[0]
        return max([e for e in matching if e.startswith(first_year)]), None
    return None, f"No expiry dates found in month '{expiry_month}' for {symbol}."

def prepare_evaluated_order(cfg):
    token = get_valid_token()
    if not token:
        return None, "Upstox token unavailable or authentication paused."

    headers = {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    symbol = cfg["symbol"]
    expiry_date, exp_err = resolve_monthly_expiry_date(symbol, cfg["expiry_month"], headers)
    if exp_err or not expiry_date:
        return None, exp_err or "Could not resolve monthly expiry date."

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    _, chain_data = fetch_single_expiry_chain(instrument_key, expiry_date, headers, use_cache=True)
    if not chain_data:
        return None, f"Empty option chain returned for {symbol} expiry {expiry_date}."

    opt_type = cfg["option_type"]
    req_strike = cfg["strike_price"]
    qty = cfg["quantity"]
    max_invest = cfg["amount_to_invest"]

    if qty <= 0:
        return None, "Invalid quantity (0) in DASHBOARD cell B24."

    candidates = []
    for item in chain_data:
        st = item.get("strike_price", 0)
        leg = (item.get("call_options", {}) if opt_type == "CE" else item.get("put_options", {})) or {}
        mkt = leg.get("market_data", {}) or {}
        ltp = float(mkt.get("ltp", 0) or 0)
        inst_token = leg.get("instrument_key", "")
        if inst_token and ltp > 0:
            candidates.append({"strike": st, "ltp": ltp, "instrument_token": inst_token})

    if not candidates:
        return None, f"No active {opt_type} contracts with live LTP found for {symbol} ({expiry_date})."

    if opt_type == "CE":
        ordered = sorted([c for c in candidates if c["strike"] >= req_strike], key=lambda x: x["strike"])
    else:
        ordered = sorted([c for c in candidates if c["strike"] <= req_strike], key=lambda x: x["strike"], reverse=True)
    if not ordered:
        ordered = sorted(candidates, key=lambda x: abs(x["strike"] - req_strike))

    selected, stepped = None, []
    for c in ordered:
        total_cost = round(c["ltp"] * qty, 2)
        if max_invest <= 0 or total_cost <= max_invest:
            selected = c
            selected["total_cost"] = total_cost
            break
        stepped.append(f"{int(c['strike'])} (₹{total_cost:,.0f})")

    if not selected:
        return None, f"Budget Exceeded: Even furthest {opt_type} strike exceeds ₹{max_invest:,.0f}. Checked: {', '.join(stepped[:4])}"

    entry_price = round_tick(selected["ltp"])
    total_investment = round(entry_price * qty, 2)
    txn_type = cfg["transaction_type"]

    sl_percent = cfg["sl_percent"]
    sl_price = None
    if sl_percent is not None and sl_percent > 0:
        sl_price = round_tick(entry_price * (1.0 - sl_percent / 100.0)) if txn_type == "BUY" else round_tick(entry_price * (1.0 + sl_percent / 100.0))

    target_percent = cfg["target_percent"]
    target_price = None
    if target_percent is not None and target_percent > 0:
        target_price = round_tick(entry_price * (1.0 + target_percent / 100.0)) if txn_type == "BUY" else round_tick(entry_price * (1.0 - target_percent / 100.0))

    strike_adjusted = (int(selected["strike"]) != int(req_strike))
    signature = f"{cfg['manual_order']}|{symbol}|{expiry_date}|{int(selected['strike'])}|{opt_type}|{txn_type}|{qty}|{cfg['product']}|{sl_percent}|{target_percent}"
    duplicate_key = f"{symbol}|{opt_type}|{txn_type}"

    return {
        "signature": signature,
        "duplicate_key": duplicate_key,
        "manual_order": cfg["manual_order"],
        "want_to_trade": cfg["want_to_trade"],
        "symbol": symbol,
        "expiry_month": cfg["expiry_month"],
        "expiry_date": expiry_date,
        "requested_strike": int(req_strike),
        "selected_strike": int(selected["strike"]),
        "strike_adjusted": strike_adjusted,
        "option_type": opt_type,
        "transaction_type": txn_type,
        "quantity": qty,
        "product": cfg["product"],
        "product_label": "Normal / Delivery (D)" if cfg["product"] == "D" else "Intraday (I)",
        "instrument_token": selected["instrument_token"],
        "entry_price": entry_price,
        "amount_to_invest": max_invest,
        "total_investment": total_investment,
        "sl_percent": sl_percent,
        "sl_price": sl_price,
        "target_percent": target_percent,
        "target_price": target_price
    }, None

def submit_upstox_evaluated_order(evaluated_order):
    token = get_valid_token()
    if not token:
        return False, "Authentication token missing or paused.", False

    headers = {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    proxies = get_upstox_proxies()
    sl_price = evaluated_order.get("sl_price")
    target_price = evaluated_order.get("target_price")
    entry_price = evaluated_order["entry_price"]
    qty = evaluated_order["quantity"]
    product = evaluated_order["product"]
    inst_token = evaluated_order["instrument_token"]
    txn_type = evaluated_order["transaction_type"]

    try:
        if sl_price is not None or target_price is not None:
            rules = [{"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": entry_price}]
            if target_price is not None:
                rules.append({"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": target_price})
            if sl_price is not None:
                rules.append({"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": sl_price})

            res = HTTP_SESSION.post(
                "https://api.upstox.com/v3/order/gtt/place",
                json={"type": "MULTIPLE", "quantity": qty, "product": product, "rules": rules, "instrument_token": inst_token, "transaction_type": txn_type},
                headers=headers,
                proxies=proxies,
                timeout=(2.5, 5.0)
            )
            rj = res.json() if res.content else {}
            if res.status_code == 200 and rj.get("status") == "success":
                gtt_ids = rj.get("data", {}).get("gtt_order_ids", [])
                tsl_state = load_dynamic_tsl_state()
                tsl_state.setdefault("by_token", {})[inst_token] = {
                    "side": txn_type, "entry_price": entry_price, "best_price": entry_price,
                    "sl_percent": evaluated_order.get("sl_percent"), "target_percent": evaluated_order.get("target_percent"),
                    "current_sl": sl_price, "current_target": target_price
                }
                save_dynamic_tsl_state(tsl_state)
                return True, f"GTT Multi-Leg Order Placed! ID(s): {', '.join(gtt_ids) if gtt_ids else 'Confirmed'} | Entry: ₹{entry_price} | Dynamic SL: {sl_price or 'Blank'} | Dynamic Target: {target_price or 'Blank'}", False
            err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
            if res.status_code == 403 and "static IP" in err:
                return False, f"Upstox Static IP Block (403): Outbound IP '{get_current_outbound_ip()}' is not registered.", True
            return False, f"Upstox GTT Order Rejected ({res.status_code}): {err}", False
        else:
            res = HTTP_SESSION.post(
                "https://api-hft.upstox.com/v3/order/place",
                json={"quantity": qty, "product": product, "validity": "DAY", "price": entry_price, "tag": "DASHBOARD_ORDER", "instrument_token": inst_token, "order_type": "LIMIT", "transaction_type": txn_type, "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False},
                headers=headers,
                proxies=proxies,
                timeout=(2.5, 5.0)
            )
            rj = res.json() if res.content else {}
            if res.status_code == 200 and rj.get("status") == "success":
                oids = rj.get("data", {}).get("order_ids", [])
                oid = oids[0] if oids else rj.get("data", {}).get("order_id", "Confirmed")
                return True, f"Limit Order Placed! Order ID: {oid} | Entry: ₹{entry_price}", False
            err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
            if res.status_code == 403 and "static IP" in err:
                return False, f"Upstox Static IP Block (403): Outbound IP '{get_current_outbound_ip()}' is not registered.", True
            return False, f"Upstox Limit Order Rejected ({res.status_code}): {err}", False
    except Exception as e:
        return False, f"Order API Exception: {e}", False

def extract_symbol_and_opt_type(trading_symbol):
    ts = str(trading_symbol or "").strip().upper()
    if not ts:
        return None, None
    underlying = "BANKNIFTY" if "BANKNIFTY" in ts else ("NIFTY" if (ts.startswith("NIFTY") or re.search(r'\bNIFTY\b', ts)) else None)
    opt_match = re.search(r'(?:^|\s|\d)(CE|PE)(?:\s|$|\d)', ts)
    return underlying, (opt_match.group(1) if opt_match else None)

def evaluate_trade_guards(target_symbol, target_opt_type, max_active_trades, positions, orders, gtt_orders=None, recent_submissions=None):
    gtt_orders = gtt_orders or []
    recent_submissions = recent_submissions or {}
    now_ts = get_ist_now().timestamp()

    t_sym = str(target_symbol or "").strip().upper()
    t_opt = str(target_opt_type or "").strip().upper()
    active_items, open_pos_syms = [], {}

    for p in positions:
        qty = p.get("quantity", 0) or 0
        ts = str(p.get("trading_symbol", "")).strip().upper()
        if qty != 0 and ts:
            open_pos_syms[ts] = qty
            u, opt = extract_symbol_and_opt_type(ts)
            active_items.append({"source": "Active Position", "trading_symbol": ts, "underlying": u, "option_type": opt})

    for o in orders:
        st = str(o.get("status", "")).strip().lower()
        ts = str(o.get("trading_symbol", "")).strip().upper()
        txn = str(o.get("transaction_type", "")).strip().upper()
        if st in OPEN_ORDER_STATUSES and ts:
            pos_qty = open_pos_syms.get(ts, 0)
            if not ((pos_qty > 0 and txn == "SELL") or (pos_qty < 0 and txn == "BUY")):
                u, opt = extract_symbol_and_opt_type(ts)
                active_items.append({"source": f"Open Order ({o.get('status', '')})", "trading_symbol": ts, "underlying": u, "option_type": opt})

    for g in gtt_orders:
        st = str(g.get("status", "")).strip().lower()
        ts = str(g.get("trading_symbol", "")).strip().upper()
        if st in OPEN_ORDER_STATUSES and ts and ts not in open_pos_syms:
            u, opt = extract_symbol_and_opt_type(ts)
            active_items.append({"source": f"Active GTT ({g.get('status', '')})", "trading_symbol": ts, "underlying": u, "option_type": opt})

    active_count = len(active_items)

    dups = [i for i in active_items if i["underlying"] == t_sym and i["option_type"] == t_opt]
    if dups:
        m = dups[0]
        return {
            "allowed": False,
            "active_count": active_count,
            "max_active_trades": max_active_trades,
            "reason": f"Duplicate Blocked: {t_sym} {t_opt} already active/open ({m['trading_symbol']} - {m['source']})."
        }

    latest_order_ts = 0.0
    latest_order_label = ""
    has_executed_history = False

    for o in orders:
        st = str(o.get("status", "")).strip().lower()
        ts = str(o.get("trading_symbol", "")).strip().upper()
        if not ts or st in FAILED_ORDER_STATUSES:
            continue
        u, opt = extract_symbol_and_opt_type(ts)
        if u == t_sym and opt == t_opt:
            has_executed_history = True
            ots = parse_upstox_order_timestamp(o.get("order_timestamp", ""))
            if ots > latest_order_ts:
                latest_order_ts = ots
                latest_order_label = f"{ts} ({o.get('transaction_type', '')} - {o.get('status', '')})"

    for k, sub_ts in recent_submissions.items():
        parts = str(k).split("|")
        if len(parts) >= 2 and parts[0].upper() == t_sym and parts[1].upper() == t_opt:
            sub_f = float(sub_ts or 0)
            effective_window = DUPLICATE_COOLDOWN_SECONDS if has_executed_history else 30
            if (now_ts - sub_f) < effective_window and sub_f > latest_order_ts:
                latest_order_ts = sub_f
                latest_order_label = f"{t_sym} {t_opt} (Recent Order)"

    if latest_order_ts > 0:
        elapsed = now_ts - latest_order_ts
        if 0 <= elapsed < DUPLICATE_COOLDOWN_SECONDS:
            remaining = DUPLICATE_COOLDOWN_SECONDS - elapsed
            last_ist_str = datetime.fromtimestamp(latest_order_ts, tz=IST).strftime("%H:%M:%S IST")
            return {
                "allowed": False,
                "active_count": active_count,
                "max_active_trades": max_active_trades,
                "reason": (
                    f"1-Hr Cooldown Active: {t_sym} {t_opt} last traded at {last_ist_str} "
                    f"({latest_order_label}). Wait {format_duration_hm(remaining)} for 1h gap."
                )
            }

    if max_active_trades is not None and active_count >= max_active_trades:
        return {
            "allowed": False,
            "active_count": active_count,
            "max_active_trades": max_active_trades,
            "reason": f"Active Trade Limit Reached ({active_count}/{max_active_trades} active). Close existing trade/open order first."
        }

    return {
        "allowed": True,
        "active_count": active_count,
        "max_active_trades": max_active_trades,
        "reason": f"Eligible ({active_count}/{max_active_trades} active trades)."
    }

# --- ULTRA-FAST VECTORIZED GOOGLE SHEET PARSER & SINGLE-FLIGHT CACHE ---
_DT_SPLIT_RE = re.compile(r'[\sT]+')
_DATE_PART_RE = re.compile(r'^(\d{1,4})[/\-](\d{1,2})[/\-](\d{1,4})$')

def _detect_date_order(sample_strings):
    for raw in sample_strings:
        if not raw:
            continue
        date_tok = _DT_SPLIT_RE.split(raw.strip(), 1)[0]
        m = _DATE_PART_RE.match(date_tok)
        if not m:
            continue
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 1000:
            return "YMD"
        if a > 12:
            return "DMY"
        if b > 12:
            return "MDY"
    return "DMY"

def parse_ist_datetime_fast(dt_str, date_order="DMY"):
    if not dt_str:
        return 0, "", ""
    s = dt_str.strip()
    parts = _DT_SPLIT_RE.split(s)
    date_tok = parts[0]
    m = _DATE_PART_RE.match(date_tok)
    if not m:
        return 0, date_tok, parts[1] if len(parts) > 1 else date_tok

    p1, p2, p3 = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if date_order == "YMD" or p1 > 1000:
        yr, mo, dy = p1, p2, p3
    elif date_order == "MDY":
        mo, dy, yr = p1, p2, p3
    else:
        dy, mo, yr = p1, p2, p3

    if yr < 100:
        yr += 2000

    hr = mn = sc = 0
    formatted_time = ""
    if len(parts) >= 2:
        t_tok = parts[1]
        t_nums = t_tok.split(":")
        try:
            hr = int(t_nums[0])
            mn = int(t_nums[1]) if len(t_nums) > 1 else 0
            sc = int(float(t_nums[2])) if len(t_nums) > 2 else 0
        except ValueError:
            hr = mn = sc = 0

        if len(parts) >= 3:
            ampm = parts[2].upper()
            if ampm == "PM" and hr < 12:
                hr += 12
            elif ampm == "AM" and hr == 12:
                hr = 0

        formatted_time = f"{hr:02d}:{mn:02d}"

    canonical_date = f"{dy:02d}/{mo:02d}/{yr:04d}"
    if not formatted_time:
        formatted_time = canonical_date

    try:
        ts = int(datetime(yr, mo, dy, hr, mn, sc, tzinfo=IST).timestamp())
        return ts, canonical_date, formatted_time
    except ValueError:
        return 0, canonical_date, formatted_time

def _download_and_parse_gsheet(symbol, sheet_name, expected_kind="history"):
    t0 = time.time()
    sheet_id = SPREADSHEET_ID or "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM"
    sym_clean = "BANKNIFTY" if "BANK" in symbol.upper() else "NIFTY"

    if sheet_name:
        candidate_sheets = list(dict.fromkeys([sheet_name, sheet_name.upper()]))
    else:
        candidate_sheets = [sym_clean]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    raw_csv, used_sheet, last_err = None, None, None

    for s_name in candidate_sheets:
        cb = int(time.time() * 1000)
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(s_name)}&_cb={cb}"
        try:
            res = HTTP_SESSION.get(url, headers=headers, timeout=(2.5, 4.5))
            if res.status_code == 200:
                text = res.text.strip().lstrip('\ufeff')
                if text.startswith("<!DOCTYPE") or "<html" in text.lower():
                    last_err = "Google Sheet is private. Set sharing to 'Anyone with the link can view'."
                    continue
                if len(text) > 30:
                    non_empty_head = [l for l in text.splitlines() if l.strip()][:15]
                    head_sample = "\n".join(non_empty_head).lower()
                    if "want to trade" in head_sample or "manual order" in head_sample:
                        last_err = f"Tab '{s_name}' not found in Google Sheet (fell back to DASHBOARD)."
                        continue
                    if expected_kind == "signal" and not any(k in head_sample for k in ("min roc", "max roc", "main volatility", "cross roc")):
                        last_err = f"Tab '{s_name}' missing ROC/Volatility signal columns."
                        continue
                    if expected_kind == "history" and not any(k in head_sample for k in ("pcr", "ce oi", "price ltp", "atm strike")):
                        last_err = f"Tab '{s_name}' missing Option Chain history columns."
                        continue
                    raw_csv = text
                    used_sheet = s_name
                    break
            else:
                last_err = f"Google Sheet HTTP status {res.status_code}"
        except Exception as e:
            last_err = f"Google Sheet timeout/error: {e}"

    if not raw_csv:
        LAST_DIAGNOSTICS["gsheet_ms"] = int((time.time() - t0) * 1000)
        return [], None, last_err or f"Could not retrieve '{sheet_name or sym_clean}' from Google Sheet."

    try:
        lines = [l.strip().lstrip('\ufeff') for l in raw_csv.splitlines() if l.strip()]
        if not lines:
            return [], used_sheet, "Google Sheet CSV is empty."

        header_idx = 0
        for i, l in enumerate(lines[:15]):
            ll = l.lower()
            if any(k in ll for k in ("time", "symbol", "pcr", "ltp", "min roc")):
                header_idx = i
                break

        parsed_rows = list(csv.reader(io.StringIO("\n".join(lines[header_idx:]))))
        if len(parsed_rows) < 2:
            return [], used_sheet, f"Google Sheet connected ({used_sheet}) but no data rows found."

        header_cells = parsed_rows[0]
        norm_headers = [re.sub(r'^[^\w]+', '', str(h)).strip().lower() for h in header_cells]
        exact_map = {}
        nospace_map = {}
        for idx, h in enumerate(norm_headers):
            if h:
                if h not in exact_map:
                    exact_map[h] = idx
                ns = h.replace(" ", "")
                if ns not in nospace_map:
                    nospace_map[ns] = idx

        def find_col(names, fallback_idx=None):
            for n in names:
                nl = n.lower()
                if nl in exact_map:
                    return exact_map[nl]
                ns = nl.replace(" ", "")
                if ns in nospace_map:
                    return nospace_map[ns]
            return fallback_idx

        idx_time = find_col(["time", "datetime", "timestamp", "date & time", "date and time"], 0)
        idx_sym = find_col(["symbol"], 1)

        data_rows = parsed_rows[1:]
        sample_times = [
            r[idx_time].strip() for r in data_rows[:80]
            if idx_time is not None and idx_time < len(r) and r[idx_time].strip()
        ]
        date_order = _detect_date_order(sample_times)

        def col_num(row, idx):
            if idx is None or idx >= len(row):
                return 0.0
            v = row[idx].strip()
            if not v:
                return 0.0
            try:
                return float(v.replace(",", "")) if "," in v else float(v)
            except ValueError:
                return 0.0

        snapshots = []
        if expected_kind == "signal":
            idx_min_roc = find_col(["min roc (buy)", "min_roc", "min roc"])
            idx_max_roc = find_col(["max roc (sell)", "max_roc", "max roc"])
            idx_cross_roc = find_col(["cross roc sig", "cross_roc_sig", "cross roc"])
            idx_main_vol = find_col(["main volatility", "main_vol", "volatility"])
            idx_new_sig = find_col(["new signal", "signal", "sig"])

            for row_idx, row_cells in enumerate(data_rows):
                if not row_cells or idx_time is None or idx_time >= len(row_cells):
                    continue
                time_val = row_cells[idx_time].strip()
                if not time_val or time_val.lower() in ("nan", "none", "0.0"):
                    continue
                ts, date_str, formatted_time = parse_ist_datetime_fast(time_val, date_order)
                sym_val = row_cells[idx_sym].strip() if (idx_sym is not None and idx_sym < len(row_cells) and row_cells[idx_sym].strip()) else sym_clean
                new_sig_val = row_cells[idx_new_sig].strip().upper() if (idx_new_sig is not None and idx_new_sig < len(row_cells)) else ""

                snapshots.append({
                    "_row_idx": row_idx,
                    "timestamp": ts,
                    "datetime": time_val,
                    "date_str": date_str,
                    "formatted_time": formatted_time,
                    "symbol": sym_val,
                    "min_roc": col_num(row_cells, idx_min_roc),
                    "max_roc": col_num(row_cells, idx_max_roc),
                    "cross_roc": col_num(row_cells, idx_cross_roc),
                    "main_volatility": col_num(row_cells, idx_main_vol),
                    "new_signal": new_sig_val
                })
        else:
            idx_spot = find_col(["price ltp", "spot_price", "ltp"])
            idx_atm = find_col(["atm strike", "atm_strike"])
            idx_pcr = find_col(["pcr"])
            idx_ce_oi = find_col(["ce oi"])
            idx_pe_oi = find_col(["pe oi"])
            idx_ce_oi_chg = find_col(["ce chg in oi", "ce chng oi", "ce chg oi"])
            idx_pe_oi_chg = find_col(["pe chg in oi", "pe chng oi", "pe chg oi"])
            idx_ce_vol = find_col(["ce vol"])
            idx_pe_vol = find_col(["pe vol"])
            idx_ce_iv = find_col(["ce iv"])
            idx_pe_iv = find_col(["pe iv"])
            idx_ce_delta = find_col(["ce delta"])
            idx_pe_delta = find_col(["pe delta"])
            idx_ce_theta = find_col(["ce theta"])
            idx_pe_theta = find_col(["pe theta"])
            idx_ce_vega = find_col(["ce vega"])
            idx_pe_vega = find_col(["pe vega"])
            idx_ce_gamma = find_col(["ce gamma"])
            idx_pe_gamma = find_col(["pe gamma"])
            idx_ce_bid = find_col(["ce bid"])
            idx_pe_bid = find_col(["pe bid"])
            idx_ce_ask = find_col(["ce ask"])
            idx_pe_ask = find_col(["pe ask"])
            idx_ce_ltp = find_col(["ce ltp"])
            idx_pe_ltp = find_col(["pe ltp"])
            idx_min_roc = find_col(["min roc (buy)", "min_roc", "min roc"])
            idx_max_roc = find_col(["max roc (sell)", "max_roc", "max roc"])
            idx_cross_roc = find_col(["cross roc sig", "cross_roc_sig", "cross roc"])
            idx_main_vol = find_col(["main volatility", "main_vol", "volatility"])
            idx_new_sig = find_col(["new signal", "signal", "sig"])

            idx_sig_5m = find_col(["5 min signal", "5min signal", "5_min_signal", "5m signal", "5 min sig"], 31)
            idx_sig_15m = find_col(["15 min signal", "15min signal", "15_min_signal", "15m signal", "15 min sig"], 32)
            idx_sig_30m = find_col(["30 min signal", "30min signal", "30_min_signal", "30m signal", "30 min sig"], 33)
            idx_sig_1h = find_col(["1 hr signal", "1hr signal", "1_hr_signal", "1 hour signal", "1h signal", "60 min signal", "1 hr sig"], 34)

            def col_sig(row, primary_idx, pos_idx):
                if pos_idx < len(row):
                    pv = row[pos_idx].strip().upper()
                    if pv in ("B", "BE", "S", "SE"):
                        return pv
                if primary_idx is not None and primary_idx < len(row):
                    nv = row[primary_idx].strip().upper()
                    if nv in ("B", "BE", "S", "SE"):
                        return nv
                return ""

            for row_idx, row_cells in enumerate(data_rows):
                if not row_cells or idx_time is None or idx_time >= len(row_cells):
                    continue
                time_val = row_cells[idx_time].strip()
                if not time_val or time_val.lower() in ("nan", "none", "0.0"):
                    continue

                ts, date_str, formatted_time = parse_ist_datetime_fast(time_val, date_order)
                sym_val = row_cells[idx_sym].strip() if (idx_sym is not None and idx_sym < len(row_cells) and row_cells[idx_sym].strip()) else sym_clean
                new_sig_val = row_cells[idx_new_sig].strip().upper() if (idx_new_sig is not None and idx_new_sig < len(row_cells)) else ""

                snapshots.append({
                    "_row_idx": row_idx,
                    "timestamp": ts,
                    "datetime": time_val,
                    "date_str": date_str,
                    "formatted_time": formatted_time,
                    "symbol": sym_val,
                    "spot_price": col_num(row_cells, idx_spot),
                    "atm_strike": col_num(row_cells, idx_atm),
                    "pcr": col_num(row_cells, idx_pcr),
                    "ce_oi": col_num(row_cells, idx_ce_oi),
                    "pe_oi": col_num(row_cells, idx_pe_oi),
                    "ce_oi_chg": col_num(row_cells, idx_ce_oi_chg),
                    "pe_oi_chg": col_num(row_cells, idx_pe_oi_chg),
                    "ce_vol": col_num(row_cells, idx_ce_vol),
                    "pe_vol": col_num(row_cells, idx_pe_vol),
                    "ce_iv": col_num(row_cells, idx_ce_iv),
                    "pe_iv": col_num(row_cells, idx_pe_iv),
                    "ce_delta": col_num(row_cells, idx_ce_delta),
                    "pe_delta": col_num(row_cells, idx_pe_delta),
                    "ce_theta": col_num(row_cells, idx_ce_theta),
                    "pe_theta": col_num(row_cells, idx_pe_theta),
                    "ce_vega": col_num(row_cells, idx_ce_vega),
                    "pe_vega": col_num(row_cells, idx_pe_vega),
                    "ce_gamma": col_num(row_cells, idx_ce_gamma),
                    "pe_gamma": col_num(row_cells, idx_pe_gamma),
                    "ce_bid": col_num(row_cells, idx_ce_bid),
                    "pe_bid": col_num(row_cells, idx_pe_bid),
                    "ce_ask": col_num(row_cells, idx_ce_ask),
                    "pe_ask": col_num(row_cells, idx_pe_ask),
                    "ce_ltp": col_num(row_cells, idx_ce_ltp),
                    "pe_ltp": col_num(row_cells, idx_pe_ltp),
                    "min_roc": col_num(row_cells, idx_min_roc),
                    "max_roc": col_num(row_cells, idx_max_roc),
                    "cross_roc": col_num(row_cells, idx_cross_roc),
                    "main_volatility": col_num(row_cells, idx_main_vol),
                    "new_signal": new_sig_val,
                    "signal_5m": col_sig(row_cells, idx_sig_5m, 31),
                    "signal_15m": col_sig(row_cells, idx_sig_15m, 32),
                    "signal_30m": col_sig(row_cells, idx_sig_30m, 33),
                    "signal_1h": col_sig(row_cells, idx_sig_1h, 34)
                })

        if not snapshots:
            return [], used_sheet, f"Google Sheet connected ({used_sheet}) but no valid data rows were found."

        if any(s["timestamp"] > 0 for s in snapshots):
            snapshots.sort(key=lambda x: (x["timestamp"], x["_row_idx"]))

        LAST_DIAGNOSTICS["gsheet_ms"] = int((time.time() - t0) * 1000)
        return snapshots, used_sheet, None
    except Exception as e:
        LAST_DIAGNOSTICS["gsheet_ms"] = int((time.time() - t0) * 1000)
        return [], used_sheet, f"Parsing error reading Google Sheet CSV: {e}"

def _get_sheet_lock(cache_key):
    with CACHE_LOCK:
        lk = HISTORY_INFLIGHT_LOCKS.get(cache_key)
        if lk is None:
            lk = threading.Lock()
            HISTORY_INFLIGHT_LOCKS[cache_key] = lk
        return lk

def fetch_history_from_gsheet(symbol="NIFTY", sheet_name=None, expected_kind="history", force_fresh=False):
    sym_clean = "BANKNIFTY" if "BANK" in symbol.upper() else "NIFTY"
    target_tab = (sheet_name or sym_clean).upper()
    cache_key = f"{sym_clean}|{target_tab}|{expected_kind}"
    ACTIVE_ANALYSIS_SYMBOLS[sym_clean] = time.time()

    # Coalescing threshold: 2.5s when force=1 (prevents duplicate downloads if background sync just fetched it), 8.5s for dropdown switches
    fresh_ttl = 2.5 if force_fresh else 8.5
    now = time.time()

    with CACHE_LOCK:
        cached = HISTORY_CACHE.get(cache_key)
        if cached and cached["snapshots"] and (now - cached["ts"]) < fresh_ttl:
            return cached["snapshots"], cached.get("sheet_used", target_tab), None

    sheet_lock = _get_sheet_lock(cache_key)
    with sheet_lock:
        # Re-check cache after acquiring single-flight lock (another thread may have just finished downloading)
        now2 = time.time()
        with CACHE_LOCK:
            cached = HISTORY_CACHE.get(cache_key)
            if cached and cached["snapshots"] and (now2 - cached["ts"]) < fresh_ttl:
                return cached["snapshots"], cached.get("sheet_used", target_tab), None

        ordered_snaps, used_sheet, err = _download_and_parse_gsheet(sym_clean, target_tab, expected_kind=expected_kind)
        if err or not ordered_snaps:
            with CACHE_LOCK:
                if cache_key in HISTORY_CACHE and HISTORY_CACHE[cache_key]["snapshots"]:
                    c = HISTORY_CACHE[cache_key]
                    return c["snapshots"], c.get("sheet_used", target_tab), None
            return [], used_sheet or target_tab, err or "Could not retrieve CSV from Google Sheet."

        with CACHE_LOCK:
            HISTORY_CACHE[cache_key] = {"ts": time.time(), "snapshots": ordered_snaps, "sheet_used": used_sheet}
        return ordered_snaps, used_sheet, None

def _background_sheet_keepalive_loop():
    """Proactively refreshes active Google Sheet tabs every 8s so frontend 10s Auto Sync resolves in <10ms."""
    while True:
        try:
            now = time.time()
            active_syms = [s for s, ts in list(ACTIVE_ANALYSIS_SYMBOLS.items()) if (now - ts) < 180]
            if not active_syms:
                active_syms = ["NIFTY"]
            tasks = []
            for sym in active_syms[:2]:
                tasks.append((sym, sym, "history"))
                tasks.append((sym, f"{sym}_5_MIN", "signal"))
            with ThreadPoolExecutor(max_workers=4) as ex:
                for sym, tab, kind in tasks:
                    ex.submit(fetch_history_from_gsheet, sym, tab, kind, True)
        except Exception:
            pass
        time.sleep(8.0)

threading.Thread(target=_background_sheet_keepalive_loop, daemon=True).start()

# --- PARALLEL UPSTOX POSITIONS, ORDERS & GTT FETCHER ---
def fetch_upstox_positions():
    t0 = time.time()
    token = get_valid_token()
    if not token:
        LAST_DIAGNOSTICS["upstox_status"] = "NO_TOKEN"
        return [], [], [], 0.0, 0.0, 0.0, "Authentication paused or invalid."

    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    proxies = get_upstox_proxies()

    def _get_endpoint(url):
        return HTTP_SESSION.get(url, headers=headers, proxies=proxies, timeout=(2.5, 4.5))

    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            fut_pos = ex.submit(_get_endpoint, "https://api.upstox.com/v2/portfolio/short-term-positions")
            fut_ord = ex.submit(_get_endpoint, "https://api.upstox.com/v2/order/retrieve-all")
            fut_gtt = ex.submit(_get_endpoint, "https://api.upstox.com/v3/order/gtt")
            pos_res = fut_pos.result()
            ord_res = fut_ord.result()
            gtt_res = fut_gtt.result()

        if pos_res.status_code == 401:
            invalidate_session()
            token = perform_single_auth(force_refresh=True)
            if not token:
                LAST_DIAGNOSTICS["upstox_status"] = "AUTH_EXPIRED"
                return [], [], [], 0.0, 0.0, 0.0, "Session expired."
            headers["Authorization"] = f"Bearer {token}"
            with ThreadPoolExecutor(max_workers=3) as ex:
                fut_pos = ex.submit(_get_endpoint, "https://api.upstox.com/v2/portfolio/short-term-positions")
                fut_ord = ex.submit(_get_endpoint, "https://api.upstox.com/v2/order/retrieve-all")
                fut_gtt = ex.submit(_get_endpoint, "https://api.upstox.com/v3/order/gtt")
                pos_res = fut_pos.result()
                ord_res = fut_ord.result()
                gtt_res = fut_gtt.result()

        positions_raw = pos_res.json().get("data", []) if pos_res.status_code == 200 else []
        orders_raw = ord_res.json().get("data", []) if ord_res.status_code == 200 else []
        gtt_raw = (gtt_res.json().get("data", []) or []) if gtt_res.status_code == 200 else []

        token_to_symbol = {}
        positions, total_pnl, realised_pnl, unrealised_pnl = [], 0.0, 0.0, 0.0
        for p in positions_raw:
            qty = p.get("quantity", 0)
            pnl = p.get("pnl", 0.0) or 0.0
            ts = p.get("trading_symbol", "")
            itok = p.get("instrument_token", "")
            if itok and ts:
                token_to_symbol[itok] = ts
            total_pnl += pnl
            unrealised_pnl += (p.get("unrealised", 0.0) or 0.0)
            realised_pnl += (p.get("realised", 0.0) or 0.0)
            positions.append({
                "trading_symbol": ts,
                "instrument_token": itok,
                "product": p.get("product", ""),
                "quantity": qty,
                "buy_price": round(p.get("buy_price", 0.0) or 0.0, 2),
                "sell_price": round(p.get("sell_price", 0.0) or 0.0, 2),
                "ltp": round(p.get("last_price", 0.0) or 0.0, 2),
                "pnl": round(pnl, 2),
                "best_price": None,
                "dynamic_sl": None,
                "dynamic_target": None,
                "status": "OPEN" if qty != 0 else "CLOSED"
            })

        orders = []
        for o in orders_raw:
            ts = o.get("trading_symbol", "")
            itok = o.get("instrument_token", "")
            if itok and ts:
                token_to_symbol[itok] = ts
            st_raw = str(o.get("status", "")).strip()
            orders.append({
                "order_id": o.get("order_id", ""),
                "trading_symbol": ts,
                "instrument_token": itok,
                "transaction_type": o.get("transaction_type", ""),
                "order_type": o.get("order_type", "LIMIT"),
                "product": o.get("product", "I"),
                "validity": o.get("validity", "DAY"),
                "quantity": o.get("quantity", 0),
                "disclosed_quantity": o.get("disclosed_quantity", 0),
                "price": o.get("price", 0.0),
                "trigger_price": o.get("trigger_price", 0.0),
                "average_price": o.get("average_price", 0.0),
                "status": st_raw,
                "is_open": st_raw.lower() in OPEN_ORDER_STATUSES,
                "order_timestamp": o.get("order_timestamp", "")
            })

        gtt_orders = []
        for g in gtt_raw:
            rules = g.get("rules", []) or []
            er = next((r for r in rules if str(r.get("strategy", "")).upper() == "ENTRY"), rules[0] if rules else {})
            tr = next((r for r in rules if str(r.get("strategy", "")).upper() == "TARGET"), None)
            sr = next((r for r in rules if str(r.get("strategy", "")).upper() == "STOPLOSS"), None)
            itok = g.get("instrument_token", "")
            g_ts = g.get("trading_symbol") or token_to_symbol.get(itok, "")
            g_status = str(er.get("status") or g.get("status", "")).strip()
            gtt_orders.append({
                "gtt_order_id": g.get("gtt_order_id", ""),
                "type": g.get("type", "MULTIPLE"),
                "instrument_token": itok,
                "trading_symbol": g_ts,
                "transaction_type": g.get("transaction_type", ""),
                "product": g.get("product", "I"),
                "quantity": g.get("quantity", 0),
                "entry_price": er.get("trigger_price", 0.0) if er else 0.0,
                "target_price": tr.get("trigger_price", None) if tr else None,
                "sl_price": sr.get("trigger_price", None) if sr else None,
                "status": g_status,
                "is_open": g_status.lower() in OPEN_ORDER_STATUSES,
                "raw_rules": rules
            })

        LAST_DIAGNOSTICS["upstox_ms"] = int((time.time() - t0) * 1000)
        LAST_DIAGNOSTICS["upstox_status"] = "OK"
        return positions, orders, gtt_orders, round(total_pnl, 2), round(realised_pnl, 2), round(unrealised_pnl, 2), None
    except Exception as e:
        LAST_DIAGNOSTICS["upstox_ms"] = int((time.time() - t0) * 1000)
        LAST_DIAGNOSTICS["upstox_status"] = f"ERR ({e})"
        return [], [], [], 0.0, 0.0, 0.0, str(e)

def _bg_refresh_chain(cache_key, instrument_key, expiry_date, headers):
    try:
        res = HTTP_SESSION.get(
            f"https://api.upstox.com/v2/option/chain?instrument_key={urllib.parse.quote(instrument_key)}&expiry_date={expiry_date}",
            headers=headers,
            timeout=(2.5, 4.0)
        )
        if res.status_code == 200:
            data = res.json().get("data", [])
            if data:
                with CACHE_LOCK:
                    CHAIN_CACHE[cache_key] = {"ts": time.time(), "data": data}
    except Exception:
        pass
    finally:
        with CACHE_LOCK:
            CHAIN_REFRESH_INFLIGHT.discard(cache_key)

def fetch_single_expiry_chain(instrument_key, expiry_date, headers, use_cache=True):
    cache_key = (instrument_key, expiry_date)
    now = time.time()
    if use_cache:
        trigger_bg = False
        stale_data = None
        with CACHE_LOCK:
            cached = CHAIN_CACHE.get(cache_key)
            if cached and cached["data"]:
                age = now - cached["ts"]
                if age < 2.5:
                    return expiry_date, cached["data"]
                if age < 15.0:
                    stale_data = cached["data"]
                    if cache_key not in CHAIN_REFRESH_INFLIGHT:
                        CHAIN_REFRESH_INFLIGHT.add(cache_key)
                        trigger_bg = True
        if stale_data is not None:
            if trigger_bg:
                threading.Thread(target=_bg_refresh_chain, args=(cache_key, instrument_key, expiry_date, dict(headers)), daemon=True).start()
            return expiry_date, stale_data

    try:
        res = HTTP_SESSION.get(
            f"https://api.upstox.com/v2/option/chain?instrument_key={urllib.parse.quote(instrument_key)}&expiry_date={expiry_date}",
            headers=headers,
            timeout=(2.5, 4.0)
        )
        if res.status_code == 200:
            data = res.json().get("data", [])
            if data:
                with CACHE_LOCK:
                    CHAIN_CACHE[cache_key] = {"ts": time.time(), "data": data}
            return expiry_date, data
    except Exception:
        pass
    with CACHE_LOCK:
        if cache_key in CHAIN_CACHE and CHAIN_CACHE[cache_key]["data"]:
            return expiry_date, CHAIN_CACHE[cache_key]["data"]
    return expiry_date, []

def fetch_option_chain(symbol, selected_expiry="ALL"):
    token = get_valid_token()
    if not token:
        return None, None, [], "Upstox token unavailable (authentication paused or missing)."
    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    try:
        expiries, exp_err = get_cached_expiries(symbol, headers)
        if exp_err or not expiries:
            return None, None, [], exp_err or "No option contract expiries found."

        clean_exp = str(selected_expiry).strip().upper() if selected_expiry else "ALL"
        if clean_exp in ("ALL", "ALL EXPIRIES (CONSOLIDATED)") or clean_exp not in expiries:
            results_dict = {}
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(fetch_single_expiry_chain, instrument_key, exp, headers, True): exp for exp in expiries[:4]}
                for fut in as_completed(futs):
                    ed, data = fut.result()
                    if data:
                        results_dict[ed] = data
            combined = []
            for exp in sorted(results_dict.keys()):
                combined.extend(results_dict[exp])
            return combined, "ALL EXPIRIES (CONSOLIDATED)", expiries, None

        _, chain_data = fetch_single_expiry_chain(instrument_key, clean_exp, headers, use_cache=True)
        return chain_data, clean_exp, expiries, None
    except Exception as e:
        return None, None, [], f"Exception during fetch: {e}"

def process_option_data(symbol, selected_expiry="ALL"):
    chain_data, current_expiry, expiries, error = fetch_option_chain(symbol, selected_expiry)
    updated_time = get_ist_now().strftime("%d/%m/%Y %H:%M:%S IST")
    if error or not chain_data:
        return [], 0, 0, current_expiry or "ALL EXPIRIES (CONSOLIDATED)", expiries, updated_time, 0, 0, 0, error or "No option chain data available."

    spot_price = chain_data[0].get("underlying_spot_price", 0) if chain_data else 0
    step = 100 if symbol == "BANKNIFTY" else 50
    atm_strike = round(spot_price / step) * step if step > 0 else spot_price
    min_s, max_s = atm_strike - (12 * step), atm_strike + (12 * step)
    strike_map = {}

    for item in chain_data:
        strike = item.get("strike_price", 0)
        if not (min_s <= strike <= max_s):
            continue
        if strike not in strike_map:
            strike_map[strike] = {
                "strike": strike, "is_atm": (strike == atm_strike),
                "ce_oi": 0, "ce_oi_chg": 0, "ce_vol": 0, "ce_bid_qty": 0, "ce_ask_qty": 0,
                "ce_iv": 0, "ce_ltp": 0, "ce_bid": 0, "ce_ask": 0, "ce_delta": 0, "ce_theta": 0, "ce_vega": 0, "ce_gamma": 0,
                "pe_oi": 0, "pe_oi_chg": 0, "pe_vol": 0, "pe_bid_qty": 0, "pe_ask_qty": 0,
                "pe_iv": 0, "pe_ltp": 0, "pe_bid": 0, "pe_ask": 0, "pe_delta": 0, "pe_theta": 0, "pe_vega": 0, "pe_gamma": 0,
                "_has_pricing": False
            }
        rec = strike_map[strike]
        ce, pe = item.get("call_options", {}) or {}, item.get("put_options", {}) or {}
        ce_mkt, ce_gk = ce.get("market_data", {}) or {}, ce.get("option_greeks", {}) or {}
        pe_mkt, pe_gk = pe.get("market_data", {}) or {}, pe.get("option_greeks", {}) or {}
        c_oi, p_oi = ce_mkt.get("oi", 0) or 0, pe_mkt.get("oi", 0) or 0

        rec["ce_oi"] += c_oi
        rec["pe_oi"] += p_oi
        rec["ce_oi_chg"] += ce_mkt.get("poi", 0) or (c_oi - ce_mkt.get("prev_oi", c_oi))
        rec["pe_oi_chg"] += pe_mkt.get("poi", 0) or (p_oi - pe_mkt.get("prev_oi", p_oi))
        rec["ce_vol"] += ce_mkt.get("volume", 0) or 0
        rec["pe_vol"] += pe_mkt.get("volume", 0) or 0
        rec["ce_bid_qty"] += ce_mkt.get("bid_qty", 0) or 0
        rec["ce_ask_qty"] += ce_mkt.get("ask_qty", 0) or 0
        rec["pe_bid_qty"] += pe_mkt.get("bid_qty", 0) or 0
        rec["pe_ask_qty"] += pe_mkt.get("ask_qty", 0) or 0

        if not rec["_has_pricing"]:
            rec["ce_ltp"] = ce_mkt.get("ltp", 0) or 0
            rec["ce_bid"] = ce_mkt.get("bid_price", 0) or 0
            rec["ce_ask"] = ce_mkt.get("ask_price", 0) or 0
            rec["ce_iv"] = round(ce_gk.get("iv", 0) or 0, 2)
            rec["ce_delta"] = round(ce_gk.get("delta", 0) or 0, 2)
            rec["ce_theta"] = round(ce_gk.get("theta", 0) or 0, 2)
            rec["ce_vega"] = round(ce_gk.get("vega", 0) or 0, 2)
            rec["ce_gamma"] = round(ce_gk.get("gamma", 0) or 0, 4)
            rec["pe_ltp"] = pe_mkt.get("ltp", 0) or 0
            rec["pe_bid"] = pe_mkt.get("bid_price", 0) or 0
            rec["pe_ask"] = pe_mkt.get("ask_price", 0) or 0
            rec["pe_iv"] = round(pe_gk.get("iv", 0) or 0, 2)
            rec["pe_delta"] = round(pe_gk.get("delta", 0) or 0, 2)
            rec["pe_theta"] = round(pe_gk.get("theta", 0) or 0, 2)
            rec["pe_vega"] = round(pe_gk.get("vega", 0) or 0, 2)
            rec["pe_gamma"] = round(pe_gk.get("gamma", 0) or 0, 4)
            rec["_has_pricing"] = True

    rows = [strike_map[k] for k in sorted(strike_map.keys())]
    t_ce, t_pe = sum(r["ce_oi"] for r in rows), sum(r["pe_oi"] for r in rows)
    pcr = round(t_pe / t_ce, 2) if t_ce > 0 else 0
    return rows, spot_price, atm_strike, current_expiry, expiries, updated_time, pcr, t_ce, t_pe, None

# --- MAIN TABLE TEMPLATE ---
MAIN_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <title>Upstox Pro Option Chain & Greeks Dashboard</title>
    <meta http-equiv="refresh" content="60">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 15px; background-color: #f4f6f9; }
        h2 { color: #1a252f; margin-bottom: 12px; }
        .info-box { background: #fff; padding: 18px; border-radius: 8px; margin-bottom: 18px; box-shadow: 0 2px 6px rgba(0,0,0,0.08); }
        .metrics-grid { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 15px; font-size: 15px; }
        .metric-card { background: #f8f9fa; padding: 10px 16px; border-radius: 6px; border-left: 4px solid #007bff; }
        .pcr-bullish { border-left-color: #28a745; color: #1e7e34; }
        .pcr-bearish { border-left-color: #dc3545; color: #bd2130; }
        .btn { display: inline-block; padding: 8px 16px; color: #fff; background-color: #007bff; text-decoration: none; border-radius: 4px; margin-right: 10px; font-weight: 500; }
        .btn-success { background-color: #28a745; }
        .btn-purple { background-color: #6f42c1; }
        select { padding: 8px 12px; font-size: 14px; border-radius: 4px; border: 1px solid #ced4da; outline: none; font-weight: 600; }
        .table-container { overflow-x: auto; background: #fff; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.06); }
        table { width: 100%; border-collapse: collapse; min-width: 1300px; }
        th, td { border: 1px solid #e9ecef; padding: 7px 9px; text-align: center; font-size: 13px; }
        th { background-color: #343a40; color: white; position: sticky; top: 0; }
        tr:nth-child(even){ background-color: #f8f9fa; }
        .ce-header { background-color: #28a745 !important; color: white; }
        .pe-header { background-color: #dc3545 !important; color: white; }
        .ce-cell { background-color: #f0fff4; }
        .pe-cell { background-color: #fff5f5; }
        .greek-col, .order-col { font-size: 12px; color: #495057; }
        .strike-normal { background-color: #e2e8f0; font-weight: bold; }
        .atm-spot { background-color: #facc15 !important; color: #000 !important; font-weight: bold; font-size: 14px; border: 2px solid #ca8a04 !important; }
        .error-msg { color: #dc3545; font-weight: bold; padding: 12px; background: #f8d7da; border-radius: 4px; }
    </style>
</head>
<body>
    <h2>Upstox Pro Option Chain & Greeks Dashboard made by Sumit Gujrathi</h2>
    <div class="info-box">
        <div class="metrics-grid">
            <div class="metric-card"><strong>Symbol:</strong> {{ symbol }}</div>
            <div class="metric-card"><strong>Spot Price:</strong> {{ spot_price }} (ATM: {{ atm_strike }})</div>
            <div class="metric-card"><strong>Expiry:</strong> {{ expiry }}</div>
            <div class="metric-card {% if pcr >= 1.0 %}pcr-bullish{% else %}pcr-bearish{% endif %}">
                <strong>PCR:</strong> {{ pcr }} {% if pcr >= 1.0 %}(Bullish){% else %}(Bearish){% endif %}
            </div>
            <div class="metric-card"><strong>Total Call OI:</strong> {{ "{:,}".format(total_ce_oi) }}</div>
            <div class="metric-card"><strong>Total Put OI:</strong> {{ "{:,}".format(total_pe_oi) }}</div>
            <div class="metric-card"><strong>Last Updated (IST):</strong> {{ updated }}</div>
        </div>
        <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
            <a href="/?symbol=NIFTY&expiry={{ expiry }}" class="btn">View NIFTY</a>
            <a href="/?symbol=BANKNIFTY&expiry={{ expiry }}" class="btn">View BANKNIFTY</a>
            <form method="GET" action="/" style="display: inline-flex; align-items: center; gap: 6px; margin: 0;">
                <input type="hidden" name="symbol" value="{{ symbol }}">
                <label style="font-weight: bold; color: #334155;">Select Expiry:</label>
                <select name="expiry" onchange="this.form.submit()">
                    <option value="ALL" {% if expiry == 'ALL EXPIRIES (CONSOLIDATED)' or expiry == 'ALL' or not expiry %}selected{% endif %}>★ ALL EXPIRIES (CONSOLIDATED)</option>
                    {% for exp in expiries %}<option value="{{ exp }}" {% if exp == expiry %}selected{% endif %}>{{ exp }}</option>{% endfor %}
                </select>
            </form>
            <a href="/analysis?symbol={{ symbol }}&expiry={{ expiry }}" class="btn btn-purple">📊 Open Analysis Dashboard</a>
            <a href="/download?symbol={{ symbol }}&expiry={{ expiry }}" class="btn btn-success">Download Full CSV</a>
        </div>
    </div>
    {% if error %}<div class="error-msg">Error: {{ error }}</div>{% else %}
    <div class="table-container">
        <table>
            <thead>
                <tr><th colspan="11" class="ce-header">CALL OPTIONS (CE)</th><th>STRIKE</th><th colspan="11" class="pe-header">PUT OPTIONS (PE)</th></tr>
                <tr><th>OI</th><th>Chg in OI</th><th>Vol</th><th>IV</th><th>Delta</th><th>Theta</th><th>Vega</th><th>Gamma</th><th>Bid</th><th>Ask</th><th>LTP</th><th>Price</th><th>LTP</th><th>Bid</th><th>Ask</th><th>Delta</th><th>Theta</th><th>Vega</th><th>Gamma</th><th>IV</th><th>Vol</th><th>Chg in OI</th><th>OI</th></tr>
            </thead>
            <tbody>
                {% for row in rows %}
                <tr>
                    <td class="ce-cell">{{ "{:,}".format(row.ce_oi) }}</td><td class="ce-cell">{{ "{:,}".format(row.ce_oi_chg) }}</td><td class="ce-cell">{{ "{:,}".format(row.ce_vol) }}</td><td class="ce-cell">{{ row.ce_iv }}</td>
                    <td class="ce-cell greek-col">{{ row.ce_delta }}</td><td class="ce-cell greek-col">{{ row.ce_theta }}</td><td class="ce-cell greek-col">{{ row.ce_vega }}</td><td class="ce-cell greek-col">{{ row.ce_gamma }}</td>
                    <td class="ce-cell order-col">{{ row.ce_bid }}</td><td class="ce-cell order-col">{{ row.ce_ask }}</td><td class="ce-cell"><strong>{{ row.ce_ltp }}</strong></td>
                    <td class="{% if row.is_atm %}atm-spot{% else %}strike-normal{% endif %}">{% if row.is_atm %}★ {{ row.strike }} (SPOT){% else %}{{ row.strike }}{% endif %}</td>
                    <td class="pe-cell"><strong>{{ row.pe_ltp }}</strong></td><td class="pe-cell order-col">{{ row.pe_bid }}</td><td class="pe-cell order-col">{{ row.pe_ask }}</td>
                    <td class="pe-cell greek-col">{{ row.pe_delta }}</td><td class="pe-cell greek-col">{{ row.pe_theta }}</td><td class="pe-cell greek-col">{{ row.pe_vega }}</td><td class="pe-cell greek-col">{{ row.pe_gamma }}</td>
                    <td class="pe-cell">{{ row.pe_iv }}</td><td class="pe-cell">{{ "{:,}".format(row.pe_vol) }}</td><td class="pe-cell">{{ "{:,}".format(row.pe_oi_chg) }}</td><td class="pe-cell">{{ "{:,}".format(row.pe_oi) }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% endif %}
</body>
</html>"""

# --- ANALYSIS DASHBOARD TEMPLATE (ULTRA-FAST 10s AUTO-SYNC WITH TIMEOUT GUARD + AREA ZOOM & PAN) ---
ANALYSIS_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <title>{{ symbol }} Option Chain & Technical Chart Analysis</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #0f172a; color: #f8fafc; }
        h2 { color: #38bdf8; margin-bottom: 5px; }
        .subtitle { color: #94a3b8; font-size: 14px; margin-bottom: 20px; }
        .nav-bar { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; align-items: center; background: #1e293b; padding: 15px; border-radius: 10px; }
        .btn { padding: 9px 18px; color: #fff; background-color: #3b82f6; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 14px; border: none; cursor: pointer; }
        .btn-table { background-color: #64748b; }
        .btn-dl { background-color: #10b981; font-size: 12px; padding: 6px 12px; text-decoration: none; color: #fff; border-radius: 6px; font-weight: bold; border: none; cursor: pointer; }
        .zoom-toolbar { display: inline-flex; align-items: center; gap: 6px; background: #0f172a; padding: 4px 8px; border-radius: 8px; border: 1px solid #334155; }
        .btn-zoom { background-color: #334155; color: #f8fafc; border: 1px solid #475569; padding: 5px 10px; border-radius: 5px; font-size: 12px; font-weight: 700; cursor: pointer; transition: background-color 0.15s; }
        .btn-zoom:hover { background-color: #475569; }
        .btn-zoom-reset { background-color: #1e293b; color: #94a3b8; border: 1px solid #475569; padding: 5px 10px; border-radius: 5px; font-size: 12px; font-weight: 700; cursor: pointer; }
        .btn-zoom-reset.active-zoom { background-color: #f59e0b; color: #0f172a; border-color: #fbbf24; }
        select { padding: 9px 14px; background: #0f172a; color: #38bdf8; border: 1px solid #334155; border-radius: 6px; font-weight: 600; font-size: 14px; outline: none; }
        .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
        .card { background: #1e293b; padding: 16px; border-radius: 10px; border-left: 5px solid #3b82f6; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
        .card-title { font-size: 12px; color: #94a3b8; text-transform: uppercase; font-weight: bold; margin-bottom: 6px; }
        .card-value { font-size: 22px; font-weight: bold; color: #f8fafc; }
        .sentiment-bar-container { background: #1e293b; border: 1px solid #334155; padding: 15px; border-radius: 10px; margin-bottom: 25px; }
        .gauge-flex { display: flex; height: 24px; border-radius: 12px; overflow: hidden; margin-top: 10px; }
        .gauge-pe { background-color: #22c55e; height: 100%; transition: width 0.4s ease; }
        .gauge-ce { background-color: #ef4444; height: 100%; transition: width 0.4s ease; }
        .chart-box { background: #1e293b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); margin-bottom: 30px; position: relative; }
        .chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 10px; }
        .chart-title { font-size: 18px; font-weight: bold; color: #f8fafc; }
        .sheet-meta-badge { background: #0f172a; border: 1px solid #334155; color: #34d399; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-family: monospace; }
        .zoom-hint { font-size: 12px; color: #94a3b8; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
        .timer-badge { background: #0284c7; color: #ffffff; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .error-msg { color: #f87171; font-weight: bold; padding: 15px; background: #450a0a; border-radius: 8px; margin-bottom: 16px; }
        .canvas-wrap { position: relative; height: 420px; width: 100%; }
    </style>
</head>
<body>
    <h2>📊 {{ symbol }} Advanced Analytics & Google Sheet Memory Dashboard</h2>
    <div class="subtitle">High-Speed 10s Direct Google Sheet Sync — Drag across any section on Chart 2 or 3 to zoom into specific dates/times</div>

    <div class="nav-bar">
        <label style="color: #94a3b8; font-weight: bold;">Symbol:</label>
        <select id="symbolSelector" onchange="switchSymbol(this.value)">
            <option value="NIFTY" {% if symbol == 'NIFTY' %}selected{% endif %}>NIFTY 50</option>
            <option value="BANKNIFTY" {% if symbol == 'BANKNIFTY' %}selected{% endif %}>BANK NIFTY</option>
        </select>

        <label style="color: #94a3b8; font-weight: bold; margin-left: 10px;">Expiry:</label>
        <select id="expirySelector" onchange="switchExpiry(this.value)">
            <option value="ALL" {% if expiry == 'ALL EXPIRIES (CONSOLIDATED)' or expiry == 'ALL' or not expiry %}selected{% endif %}>★ ALL EXPIRIES (CONSOLIDATED)</option>
            {% for exp in expiries %}<option value="{{ exp }}" {% if exp == expiry %}selected{% endif %}>{{ exp }}</option>{% endfor %}
        </select>

        <label style="color: #94a3b8; font-weight: bold; margin-left: 10px;">Bar Chart Metric:</label>
        <select id="metricSelector" onchange="updateChartMetric()">
            <option value="oi_chg" selected>Change in Open Interest (Chg in OI)</option>
            <option value="depth_qty">1 - Order Book Depth Quantity (Put Bid Qty vs Call Ask Qty)</option>
            <option value="oi">Open Interest (PE vs CE)</option>
            <option value="vol">Trading Volume</option>
            <option value="delta">Delta Exposure</option>
            <option value="theta">Theta Time Decay</option>
        </select>

        <div style="margin-left: auto; display: flex; align-items: center; gap: 12px;">
            <span class="timer-badge" id="syncBadge">Auto Sync: <span id="countdown">10</span>s</span>
            <a href="/?symbol={{ symbol }}&expiry={{ expiry }}" class="btn btn-table">📋 Back to Table View</a>
        </div>
    </div>

    <div id="errorContainer" class="error-msg" style="display: none;"></div>

    <div class="metrics-grid">
        <div class="card" style="border-left-color: #facc15;"><div class="card-title">Spot Price / ATM</div><div class="card-value" id="cardSpot" style="color: #facc15;">Loading...</div></div>
        <div class="card" id="cardPcrBox"><div class="card-title">Put-Call Ratio (PCR)</div><div class="card-value" id="cardPcr">-</div></div>
        <div class="card" style="border-left-color: #ef4444;"><div class="card-title">Major Resistance (Max Call OI)</div><div class="card-value" id="maxCallStrike" style="color: #ef4444;">-</div></div>
        <div class="card" style="border-left-color: #22c55e;"><div class="card-title">Major Support (Max Put OI)</div><div class="card-value" id="maxPutStrike" style="color: #22c55e;">-</div></div>
        <div class="card" style="border-left-color: #a855f7;"><div class="card-title">Last Updated (IST)</div><div class="card-value" id="cardUpdated" style="font-size: 15px;">Syncing...</div></div>
    </div>

    <div class="sentiment-bar-container">
        <div style="display: flex; justify-content: space-between; font-weight: bold; margin-bottom: 8px;">
            <span style="color: #22c55e;" id="gaugePeLabel">PUT (PE) TOTAL: -</span>
            <span style="color: #38bdf8;" id="gaugeMetricName">SELECTED METRIC COMPARISON</span>
            <span style="color: #ef4444;" id="gaugeCeLabel">CALL (CE) TOTAL: -</span>
        </div>
        <div class="gauge-flex"><div id="gaugePeBar" class="gauge-pe" style="width: 50%;"></div><div id="gaugeCeBar" class="gauge-ce" style="width: 50%;"></div></div>
        <div style="display: flex; justify-content: space-between; color: #94a3b8; font-size: 12px; margin-top: 6px;">
            <span id="gaugePeAvg">Put Avg: -</span><span id="gaugeRatioText">Ratio: 1.00</span><span id="gaugeCeAvg">Call Avg: -</span>
        </div>
    </div>

    <div class="chart-box">
        <div class="chart-header">
            <div class="chart-title" id="chartHeaderTitle">Change in Open Interest (Chg in OI)</div>
            <span style="color: #facc15; font-size: 13px; font-weight: bold;">★ Yellow Line = Spot ATM Strike | Dashed Lines = Linear Trendlines</span>
        </div>
        <canvas id="analysisChart" style="max-height: 480px;"></canvas>
    </div>

    <div class="chart-box">
        <div class="chart-header">
            <span class="chart-title">2 - 📈 Historical Snapshot Trend (Google Sheet Memory — B / BE / S / SE Signals)</span>
            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                <div class="zoom-toolbar">
                    <button type="button" id="histDragModeBtn" class="btn-zoom" onclick="toggleChartDragMode('history')" title="Switch between Drag-to-Zoom Area and Drag-to-Pan">🔍 Drag: Zoom</button>
                    <button type="button" class="btn-zoom" onclick="stepChartZoom('history', 1.35)" title="Zoom In">+</button>
                    <button type="button" class="btn-zoom" onclick="stepChartZoom('history', 0.74)" title="Zoom Out">−</button>
                    <button type="button" id="histResetZoomBtn" class="btn-zoom-reset" onclick="resetChartZoom('history')" title="Reset Zoom (or Double-Click Chart)">🔄 Reset Zoom</button>
                </div>
                <label style="color: #94a3b8; font-weight: bold;">Head of Dropdown:</label>
                <select id="historyMetricSelector" onchange="loadHistoricalChart(true, false)">
                    <option value="price_pcr" selected>Price LTP / PCR</option>
                    <option value="oi">Open Interest (CE OI vs PE OI)</option>
                    <option value="oi_chg">Change OI (CE Chng OI vs PE Chng OI)</option>
                    <option value="vol">Vol (CE Vol vs PE Vol)</option>
                    <option value="iv">Volatility (CE IV vs PE IV)</option>
                    <option value="delta">Delta (CE Delta vs PE Delta)</option>
                    <option value="theta">Theta (CE Theta vs PE Theta)</option>
                    <option value="vega">Vega (CE Vega vs PE Vega)</option>
                    <option value="gamma">Gamma (CE Gamma vs PE Gamma)</option>
                    <option value="bid">BID (CE Bid vs PE Bid)</option>
                    <option value="ask">ASK (CE Ask vs PE Ask)</option>
                    <option value="ltp">LTP AVG (CE LTP vs PE LTP)</option>
                </select>
                <label style="color: #94a3b8; font-weight: bold;">Data Day Range:</label>
                <select id="historyDayRangeSelector" onchange="loadHistoricalChart(true, false)">
                    <option value="1day" selected>1 Day</option><option value="3day">3 Day</option><option value="1week">1 Week</option>
                    <option value="2week">2 Week</option><option value="1month">1 Month</option><option value="3month">3 Month</option>
                </select>
                <label style="color: #94a3b8; font-weight: bold;">Interval:</label>
                <select id="historyTimeframeSelector" onchange="loadHistoricalChart(true, false)">
                    <option value="5minute" selected>5 Min</option><option value="15minute">15 Min</option><option value="30minute">30 Min</option><option value="60minute">1 Hour</option><option value="day">1 Day</option>
                </select>
                <button type="button" class="btn-zoom" onclick="loadHistoricalChart(false, true)" title="Force immediate Google Sheet sync">⚡ Sync</button>
                <a id="downloadHistoryBtn" href="/download_history?symbol={{ symbol }}" class="btn-dl">📥 CSV</a>
            </div>
        </div>
        <div class="zoom-hint">
            <span>💡 <strong>Zoom:</strong> Drag across any region • Scroll wheel at cursor • <strong>Shift + Drag</strong> (or toggle Pan) • Double-click to reset</span>
            <span id="histMetaBadge" class="sheet-meta-badge">Loading Sheet...</span>
        </div>
        <div class="canvas-wrap">
            <canvas id="historyChart" ondblclick="resetChartZoom('history')"></canvas>
        </div>
    </div>

    <div class="chart-box">
        <div class="chart-header">
            <span class="chart-title">3 - 🚀 Main Signal (B / BE / S / SE)</span>
            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                <div class="zoom-toolbar">
                    <button type="button" id="sigDragModeBtn" class="btn-zoom" onclick="toggleChartDragMode('signal')" title="Switch between Drag-to-Zoom Area and Drag-to-Pan">🔍 Drag: Zoom</button>
                    <button type="button" class="btn-zoom" onclick="stepChartZoom('signal', 1.35)" title="Zoom In">+</button>
                    <button type="button" class="btn-zoom" onclick="stepChartZoom('signal', 0.74)" title="Zoom Out">−</button>
                    <button type="button" id="sigResetZoomBtn" class="btn-zoom-reset" onclick="resetChartZoom('signal')" title="Reset Zoom (or Double-Click Chart)">🔄 Reset Zoom</button>
                </div>
                <label style="color: #94a3b8; font-weight: bold;">Data Day Range:</label>
                <select id="signalDayRangeSelector" onchange="loadSignalChart(true, false)">
                    <option value="1day" selected>1 Day</option><option value="3day">3 Day</option><option value="1week">1 Week</option>
                    <option value="2week">2 Week</option><option value="1month">1 Month</option><option value="3month">3 Month</option>
                </select>
                <label style="color: #94a3b8; font-weight: bold;">Interval:</label>
                <select id="signalTimeframeSelector" onchange="loadSignalChart(true, false)">
                    <option value="5minute" selected>5 Min</option><option value="15minute">15 Min</option><option value="30minute">30 Min</option><option value="60minute">1 Hour</option>
                </select>
                <button type="button" class="btn-zoom" onclick="loadSignalChart(false, true)" title="Force immediate Google Sheet sync">⚡ Sync</button>
            </div>
        </div>
        <div class="zoom-hint">
            <span>💡 <strong>Zoom:</strong> Drag across any region • Scroll wheel at cursor • <strong>Shift + Drag</strong> (or toggle Pan) • Double-click to reset</span>
            <span id="sigMetaBadge" class="sheet-meta-badge">Loading Sheet...</span>
        </div>
        <div class="canvas-wrap">
            <canvas id="signalChart" ondblclick="resetChartZoom('signal')"></canvas>
        </div>
    </div>

    <script>
        let currentSymbol = "{{ symbol }}";
        let currentExpiry = "{{ expiry }}";
        let rawRows = [];
        let currentAtmStrike = 0;
        let myChart = null, historyChartObj = null, signalChartObj = null;
        let hasHistoryData = false, hasSignalData = false, hasSpotData = false;
        let histAbortCtrl = null, sigAbortCtrl = null, dataAbortCtrl = null;

        const DATE_PALETTE = ['#38bdf8', '#facc15', '#4ade80', '#f472b6', '#a78bfa', '#fb923c', '#2dd4bf'];

        function fetchWithTimeout(url, externalSignal, timeoutMs = 7500) {
            const timeoutCtrl = new AbortController();
            const timerId = setTimeout(() => timeoutCtrl.abort(), timeoutMs);
            if (externalSignal) {
                if (externalSignal.aborted) {
                    clearTimeout(timerId);
                    timeoutCtrl.abort();
                } else {
                    externalSignal.addEventListener('abort', () => {
                        clearTimeout(timerId);
                        timeoutCtrl.abort();
                    }, { once: true });
                }
            }
            return fetch(url, { cache: 'no-store', signal: timeoutCtrl.signal })
                .finally(() => clearTimeout(timerId));
        }

        function updateZoomBadge(chart) {
            if (!chart) return;
            const btnId = chart === historyChartObj ? 'histResetZoomBtn' : (chart === signalChartObj ? 'sigResetZoomBtn' : null);
            if (!btnId) return;
            const btn = document.getElementById(btnId);
            if (!btn) return;
            const zoomed = typeof chart.isZoomedOrPanned === 'function' ? chart.isZoomedOrPanned() : (chart.options.scales.x.min !== undefined || chart.options.scales.x.max !== undefined);
            if (zoomed) {
                btn.classList.add('active-zoom');
            } else {
                btn.classList.remove('active-zoom');
            }
        }

        function buildInteractiveZoomOptions() {
            return {
                limits: {
                    x: { min: 'original', max: 'original', minRange: 4 }
                },
                pan: {
                    enabled: true,
                    mode: 'x',
                    modifierKey: 'shift',
                    onPanComplete: ({ chart }) => updateZoomBadge(chart)
                },
                zoom: {
                    wheel: {
                        enabled: true,
                        speed: 0.12
                    },
                    pinch: {
                        enabled: true
                    },
                    drag: {
                        enabled: true,
                        backgroundColor: 'rgba(56, 189, 248, 0.24)',
                        borderColor: '#38bdf8',
                        borderWidth: 1.5,
                        threshold: 6
                    },
                    mode: 'x',
                    onZoomComplete: ({ chart }) => updateZoomBadge(chart)
                }
            };
        }

        function toggleChartDragMode(chartKey) {
            const chart = chartKey === 'history' ? historyChartObj : signalChartObj;
            const btn = document.getElementById(chartKey === 'history' ? 'histDragModeBtn' : 'sigDragModeBtn');
            if (!chart || !chart.options.plugins.zoom) return;

            const isCurrentlyZoom = chart.options.plugins.zoom.zoom.drag.enabled;
            if (isCurrentlyZoom) {
                chart.options.plugins.zoom.zoom.drag.enabled = false;
                chart.options.plugins.zoom.pan.modifierKey = null;
                btn.innerText = '✋ Drag: Pan';
                btn.style.backgroundColor = '#0284c7';
            } else {
                chart.options.plugins.zoom.zoom.drag.enabled = true;
                chart.options.plugins.zoom.pan.modifierKey = 'shift';
                btn.innerText = '🔍 Drag: Zoom';
                btn.style.backgroundColor = '#334155';
            }
            chart.update('none');
        }

        function stepChartZoom(chartKey, factor) {
            const chart = chartKey === 'history' ? historyChartObj : signalChartObj;
            if (!chart || typeof chart.zoom !== 'function') return;
            chart.zoom(factor, 'none');
            updateZoomBadge(chart);
        }

        function resetChartZoom(chartKey) {
            const chart = chartKey === 'history' ? historyChartObj : signalChartObj;
            if (!chart) return;
            if (typeof chart.resetZoom === 'function') {
                chart.resetZoom('none');
            } else if (chart.options && chart.options.scales && chart.options.scales.x) {
                delete chart.options.scales.x.min;
                delete chart.options.scales.x.max;
                chart.update('none');
            }
            updateZoomBadge(chart);
        }

        const hoverCrosshairPlugin = {
            id: 'hoverCrosshairPlugin',
            afterDatasetsDraw: (chart) => {
                const active = chart.tooltip && chart.tooltip.getActiveElements ? chart.tooltip.getActiveElements() : [];
                if (!active || active.length === 0) return;
                const { ctx, chartArea } = chart;
                if (!chartArea) return;
                const x = active[0].element.x;
                if (x < chartArea.left || x > chartArea.right) return;
                ctx.save();
                ctx.beginPath();
                ctx.moveTo(x, chartArea.top);
                ctx.lineTo(x, chartArea.bottom);
                ctx.lineWidth = 1.5;
                ctx.strokeStyle = 'rgba(248, 250, 252, 0.55)';
                ctx.setLineDash([4, 4]);
                ctx.stroke();
                ctx.restore();
            }
        };

        function buildTimeOnlyTooltipConfig(prefixLabel) {
            return {
                enabled: true,
                mode: 'index',
                intersect: false,
                displayColors: false,
                backgroundColor: 'rgba(15, 23, 42, 0.94)',
                borderColor: '#38bdf8',
                borderWidth: 1,
                titleColor: '#38bdf8',
                titleFont: { size: 13, weight: 'bold', family: 'Segoe UI' },
                titleMarginBottom: 0,
                padding: { top: 7, bottom: 7, left: 11, right: 11 },
                callbacks: {
                    title: (items) => {
                        if (!items || !items.length) return '';
                        const idx = items[0].dataIndex;
                        const chart = items[0].chart;
                        if (chart._hoverTimes && chart._hoverTimes[idx]) {
                            return `${prefixLabel}: ${chart._hoverTimes[idx]}`;
                        }
                        return `${prefixLabel}: ${items[0].label || ''}`;
                    },
                    label: () => null
                }
            };
        }

        function buildDateMeta(snapshots) {
            const dates = [], colors = [], groups = [], hoverTimes = [];
            const dateColorMap = {};
            let paletteIdx = 0;

            snapshots.forEach((s, i) => {
                let dStr = (s.date_str || "").trim();
                if (!dStr && s.datetime && String(s.datetime).includes(" ")) {
                    dStr = String(s.datetime).split(" ")[0].trim();
                }
                const tStr = (s.formatted_time || s.datetime || "").trim();
                hoverTimes.push(dStr && dStr !== "Session" ? `${tStr} (${dStr})` : tStr);

                if (!dStr) dStr = "Session";
                if (!(dStr in dateColorMap)) {
                    dateColorMap[dStr] = DATE_PALETTE[paletteIdx % DATE_PALETTE.length];
                    paletteIdx++;
                }
                const col = dateColorMap[dStr];
                dates.push(dStr);
                colors.push(col);
                if (groups.length === 0 || groups[groups.length - 1].date !== dStr) {
                    groups.push({ date: dStr, start: i, end: i, color: col, bandIdx: groups.length });
                } else {
                    groups[groups.length - 1].end = i;
                }
            });
            return { dates, colors, groups, hoverTimes };
        }

        const dateBandPlugin = {
            id: 'dateBandPlugin',
            beforeDraw: (chart) => {
                const { ctx, chartArea, scales } = chart;
                const groups = chart._dateGroups || [];
                if (!scales || !scales.x || !chartArea || groups.length === 0) return;
                const { top, bottom, left, right } = chartArea;
                const xScale = scales.x;

                ctx.save();
                ctx.beginPath();
                ctx.rect(left, top, right - left, bottom - top);
                ctx.clip();

                groups.forEach((g, idx) => {
                    const xStart = idx === 0 ? left : (xScale.getPixelForValue(g.start - 1) + xScale.getPixelForValue(g.start)) / 2;
                    const xEnd = idx === groups.length - 1 ? right : (xScale.getPixelForValue(g.end) + xScale.getPixelForValue(g.end + 1)) / 2;
                    if (xEnd < left || xStart > right) return;

                    const visStart = Math.max(left, xStart);
                    const visEnd = Math.min(right, xEnd);

                    ctx.fillStyle = idx % 2 === 0 ? 'rgba(56, 189, 248, 0.04)' : 'rgba(250, 204, 21, 0.05)';
                    ctx.fillRect(visStart, top, visEnd - visStart, bottom - top);

                    if (idx > 0 && xStart >= left && xStart <= right) {
                        ctx.beginPath();
                        ctx.moveTo(xStart, top);
                        ctx.lineTo(xStart, bottom);
                        ctx.lineWidth = 1.5;
                        ctx.strokeStyle = 'rgba(148, 163, 184, 0.45)';
                        ctx.setLineDash([4, 4]);
                        ctx.stroke();
                        ctx.setLineDash([]);
                    }

                    if (g.date && g.date !== "Session" && (visEnd - visStart) > 48) {
                        ctx.fillStyle = g.color;
                        ctx.font = 'bold 11px Segoe UI';
                        ctx.textAlign = 'left';
                        ctx.textBaseline = 'top';
                        ctx.fillText(g.date, visStart + 6, top + 6);
                    }
                });
                ctx.restore();
            }
        };

        const signalLabelPlugin = {
            id: 'signalLabelPlugin',
            afterDatasetsDraw: (chart) => {
                const { ctx, chartArea } = chart;
                const minTop = chartArea ? chartArea.top + 2 : 2;
                const minLeft = chartArea ? chartArea.left : 0;
                const maxRight = chartArea ? chartArea.right : Infinity;

                chart.data.datasets.forEach((dataset, datasetIndex) => {
                    if (Array.isArray(dataset.signalLabels) && dataset.signalLabels.length > 0) {
                        const meta = chart.getDatasetMeta(datasetIndex);
                        const signals = dataset.signalLabels;
                        meta.data.forEach((element, index) => {
                            const rawSig = (signals[index] || "").trim().toUpperCase();
                            if (rawSig === 'B' || rawSig === 'BE' || rawSig === 'S' || rawSig === 'SE') {
                                const { x, y } = element.tooltipPosition();
                                if (x < minLeft || x > maxRight) return;

                                const badgeColor = rawSig === 'B' ? '#22c55e' : (rawSig === 'BE' ? '#f59e0b' : (rawSig === 'S' ? '#ef4444' : '#38bdf8'));
                                ctx.save();
                                ctx.font = 'bold 11px Segoe UI';
                                const textW = ctx.measureText(rawSig).width;
                                const padX = 5, boxH = 16, boxW = textW + padX * 2;
                                const boxX = x - boxW / 2;
                                const boxY = Math.max(minTop, y - boxH - 6);

                                ctx.fillStyle = '#0f172a';
                                ctx.strokeStyle = badgeColor;
                                ctx.lineWidth = 1.5;
                                ctx.beginPath();
                                ctx.roundRect(boxX, boxY, boxW, boxH, 4);
                                ctx.fill();
                                ctx.stroke();

                                ctx.fillStyle = badgeColor;
                                ctx.textAlign = 'center';
                                ctx.textBaseline = 'middle';
                                ctx.fillText(rawSig, x, boxY + boxH / 2 + 0.5);
                                ctx.restore();
                            }
                        });
                    }
                });
            }
        };

        const zeroLinePlugin = {
            id: 'zeroLinePlugin',
            beforeDraw: (chart) => {
                const { ctx, chartArea, scales } = chart;
                if (!scales || !scales.y || !chartArea) return;
                const { top, bottom, left, right } = chartArea;
                const zeroY = scales.y.getPixelForValue(0);
                ctx.save();
                if (zeroY >= top && zeroY <= bottom) {
                    ctx.fillStyle = 'rgba(34, 197, 94, 0.08)';
                    ctx.fillRect(left, top, right - left, zeroY - top);
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.08)';
                    ctx.fillRect(left, zeroY, right - left, bottom - zeroY);
                    ctx.beginPath();
                    ctx.moveTo(left, zeroY);
                    ctx.lineTo(right, zeroY);
                    ctx.lineWidth = 3;
                    ctx.strokeStyle = '#facc15';
                    ctx.stroke();
                } else if (zeroY > bottom) {
                    ctx.fillStyle = 'rgba(34, 197, 94, 0.08)';
                    ctx.fillRect(left, top, right - left, bottom - top);
                } else {
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.08)';
                    ctx.fillRect(left, top, right - left, bottom - top);
                }
                ctx.restore();
            }
        };

        function switchSymbol(newSym) {
            currentSymbol = newSym;
            document.getElementById("downloadHistoryBtn").href = `/download_history?symbol=${newSym}`;
            window.location.href = `/analysis?symbol=${newSym}&expiry=${currentExpiry}`;
        }

        function switchExpiry(newExp) {
            currentExpiry = newExp;
            fetchOptionChainData();
        }

        function computeLinearTrend(data) {
            const n = data.length;
            if (n === 0) return [];
            let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0;
            for (let i = 0; i < n; i++) {
                const val = data[i] || 0;
                sumX += i; sumY += val; sumXY += i * val; sumXX += i * i;
            }
            const denom = (n * sumXX - sumX * sumX);
            const slope = denom !== 0 ? (n * sumXY - sumX * sumY) / denom : 0;
            const intercept = (sumY - slope * sumX) / n;
            return data.map((_, i) => Number((slope * i + intercept).toFixed(2)));
        }

        function initSignalChart() {
            const ctx = document.getElementById('signalChart').getContext('2d');
            signalChartObj = new Chart(ctx, {
                type: 'line',
                data: { labels: [], datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    interaction: { mode: 'index', intersect: false },
                    hover: { mode: 'index', intersect: false },
                    scales: {
                        x: {
                            ticks: {
                                color: (c) => (c.chart._dateColors && c.chart._dateColors[c.index]) || '#94a3b8',
                                font: { weight: 'bold', size: 11 }
                            },
                            grid: { color: '#334155' }
                        },
                        y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } }
                    },
                    plugins: {
                        legend: { labels: { color: '#f8fafc', font: { size: 13 } } },
                        tooltip: buildTimeOnlyTooltipConfig('Time'),
                        zoom: buildInteractiveZoomOptions()
                    }
                },
                plugins: [dateBandPlugin, zeroLinePlugin, signalLabelPlugin, hoverCrosshairPlugin]
            });
        }

        function loadSignalChart(isManualChange = false, forceFresh = true) {
            if (sigAbortCtrl) sigAbortCtrl.abort();
            sigAbortCtrl = new AbortController();
            const dayRange = document.getElementById('signalDayRangeSelector').value;
            const timeframe = document.getElementById('signalTimeframeSelector').value;
            const metaBadge = document.getElementById('sigMetaBadge');
            const t0 = performance.now();
            if (forceFresh && metaBadge && !hasSignalData) metaBadge.innerText = "Syncing Sheet...";

            return fetchWithTimeout(
                `/api/history?symbol=${currentSymbol}&timeframe=${timeframe}&day_range=${dayRange}&chart_type=signal&force=${forceFresh ? 1 : 0}&_t=${Date.now()}`,
                sigAbortCtrl.signal,
                7500
            )
                .then(res => res.json())
                .then(data => {
                    const ms = Math.round(performance.now() - t0);
                    if (isManualChange) {
                        resetChartZoom('signal');
                    }
                    if (data.error || !(data.history && data.history.length)) {
                        if (metaBadge) {
                            metaBadge.style.color = "#f87171";
                            metaBadge.innerText = data.error || "No signal rows";
                        }
                        if (isManualChange || !hasSignalData) {
                            signalChartObj._dateColors = [];
                            signalChartObj._dateGroups = [];
                            signalChartObj._hoverTimes = [];
                            signalChartObj.data.labels = [data.error || "No data found for Main Signal."];
                            signalChartObj.data.datasets = [{ label: 'Status', data: [0], borderColor: '#ef4444' }];
                            signalChartObj.update('none');
                        }
                        return;
                    }
                    hasSignalData = true;
                    const snapshots = data.history;
                    const meta = buildDateMeta(snapshots);
                    signalChartObj._dateColors = meta.colors;
                    signalChartObj._dateGroups = meta.groups;
                    signalChartObj._hoverTimes = meta.hoverTimes;

                    const lastSnap = snapshots[snapshots.length - 1];
                    if (metaBadge) {
                        metaBadge.style.color = "#34d399";
                        metaBadge.innerText = `Tab: ${data.sheet_used || '-'} | ${snapshots.length} pts | Latest: ${lastSnap.datetime || '-'} (${ms}ms)`;
                    }

                    const times = snapshots.map(s => s.formatted_time || s.datetime);
                    signalChartObj.data.labels = times;
                    signalChartObj.data.datasets = [
                        { label: 'Min ROC (Buy)', data: snapshots.map(s => s.min_roc || 0), borderColor: '#22c55e', borderWidth: 2, pointRadius: 0, pointHoverRadius: 3, tension: 0.2 },
                        { label: 'Max ROC (Sell)', data: snapshots.map(s => s.max_roc || 0), borderColor: '#ef4444', borderWidth: 2, pointRadius: 0, pointHoverRadius: 3, tension: 0.2 },
                        { label: 'Cross ROC Sig', data: snapshots.map(s => s.cross_roc || 0), borderColor: '#facc15', borderWidth: 2, pointRadius: 0, pointHoverRadius: 3, tension: 0.2 },
                        { label: 'Main Volatility', data: snapshots.map(s => s.main_volatility || 0), signalLabels: snapshots.map(s => s.new_signal || ""), borderColor: '#38bdf8', borderWidth: 3, pointRadius: 3, pointHoverRadius: 4, tension: 0.2 }
                    ];
                    signalChartObj.update('none');
                    updateZoomBadge(signalChartObj);
                })
                .catch(err => {
                    if (err.name === 'AbortError') {
                        if (metaBadge && !hasSignalData) metaBadge.innerText = "Sheet timeout — retrying next tick";
                    } else {
                        console.error("Signal chart error:", err);
                    }
                });
        }

        function initHistoryChart() {
            const ctx = document.getElementById('historyChart').getContext('2d');
            historyChartObj = new Chart(ctx, {
                type: 'line',
                data: { labels: [], datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    interaction: { mode: 'index', intersect: false },
                    hover: { mode: 'index', intersect: false },
                    scales: {
                        x: {
                            ticks: {
                                color: (c) => (c.chart._dateColors && c.chart._dateColors[c.index]) || '#94a3b8',
                                font: { weight: 'bold', size: 11 }
                            },
                            grid: { color: '#334155' }
                        },
                        y: { position: 'left', ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
                        y1: { position: 'right', display: false, ticks: { color: '#38bdf8' }, grid: { drawOnChartArea: false } }
                    },
                    plugins: {
                        legend: { labels: { color: '#f8fafc', font: { size: 13 } } },
                        tooltip: buildTimeOnlyTooltipConfig('Time'),
                        zoom: buildInteractiveZoomOptions()
                    }
                },
                plugins: [dateBandPlugin, signalLabelPlugin, hoverCrosshairPlugin]
            });
        }

        function loadHistoricalChart(isManualChange = false, forceFresh = true) {
            if (histAbortCtrl) histAbortCtrl.abort();
            histAbortCtrl = new AbortController();

            const metric = document.getElementById('historyMetricSelector').value;
            const dayRange = document.getElementById('historyDayRangeSelector').value;
            const timeframe = document.getElementById('historyTimeframeSelector').value;
            const metaBadge = document.getElementById('histMetaBadge');
            const t0 = performance.now();
            if (forceFresh && metaBadge && !hasHistoryData) metaBadge.innerText = "Syncing Sheet...";

            const tfSignalKeyMap = {
                "5minute": "signal_5m",
                "15minute": "signal_15m",
                "30minute": "signal_30m",
                "60minute": "signal_1h",
                "day": "signal_1h"
            };
            const activeSigKey = tfSignalKeyMap[timeframe] || "signal_5m";

            return fetchWithTimeout(
                `/api/history?symbol=${currentSymbol}&timeframe=${timeframe}&metric=${metric}&day_range=${dayRange}&chart_type=history&force=${forceFresh ? 1 : 0}&_t=${Date.now()}`,
                histAbortCtrl.signal,
                7500
            )
                .then(res => res.json())
                .then(data => {
                    const ms = Math.round(performance.now() - t0);
                    if (isManualChange) {
                        resetChartZoom('history');
                    }
                    if (data.error || !(data.history && data.history.length)) {
                        if (metaBadge) {
                            metaBadge.style.color = "#f87171";
                            metaBadge.innerText = data.error || "No history rows";
                        }
                        if (isManualChange || !hasHistoryData) {
                            historyChartObj._dateColors = [];
                            historyChartObj._dateGroups = [];
                            historyChartObj._hoverTimes = [];
                            historyChartObj.data.labels = [data.error || "No data found in Google Sheet."];
                            historyChartObj.data.datasets = [{ label: 'Status', data: [0], borderColor: '#ef4444' }];
                            historyChartObj.update('none');
                        }
                        return;
                    }
                    hasHistoryData = true;
                    const snapshots = data.history;
                    const meta = buildDateMeta(snapshots);
                    historyChartObj._dateColors = meta.colors;
                    historyChartObj._dateGroups = meta.groups;
                    historyChartObj._hoverTimes = meta.hoverTimes;

                    const lastSnap = snapshots[snapshots.length - 1];
                    if (metaBadge) {
                        metaBadge.style.color = "#34d399";
                        metaBadge.innerText = `Tab: ${data.sheet_used || '-'} | ${snapshots.length} pts | Latest: ${lastSnap.datetime || '-'} (${ms}ms)`;
                    }

                    const times = snapshots.map(s => s.formatted_time || s.datetime);
                    const chart2Signals = snapshots.map(s => (s[activeSigKey] || s.sig || "").trim().toUpperCase());
                    const sigPointRadii = chart2Signals.map(sig => ['B', 'BE', 'S', 'SE'].includes(sig) ? 4.5 : 1.5);
                    let datasets = [];

                    if (metric === "price_pcr") {
                        historyChartObj.options.scales.y1.display = true;
                        const spotVals = snapshots.map(s => s.spot_price);
                        const pcrVals = snapshots.map(s => s.pcr);
                        datasets.push({ label: 'Relation 2: PCR', data: pcrVals, borderColor: '#06b6d4', borderWidth: 2, tension: 0.2, yAxisID: 'y' });
                        datasets.push({ label: 'Relation 1: Price LTP', data: spotVals, signalLabels: chart2Signals, borderColor: '#facc15', backgroundColor: 'rgba(250, 204, 21, 0.1)', borderWidth: 2, pointRadius: sigPointRadii, pointHoverRadius: 5, tension: 0.2, yAxisID: 'y1', fill: true });
                        datasets.push({ label: 'Price Trend', data: computeLinearTrend(spotVals), borderColor: '#ca8a04', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, yAxisID: 'y1' });
                    } else {
                        historyChartObj.options.scales.y1.display = false;
                        const metricMap = {
                            "oi": { key1: "ce_oi", key2: "pe_oi", r1: "CE OI", r2: "PE OI" },
                            "oi_chg": { key1: "ce_oi_chg", key2: "pe_oi_chg", r1: "CE Chng OI", r2: "PE Chng OI" },
                            "vol": { key1: "ce_vol", key2: "pe_vol", r1: "CE Vol", r2: "PE Vol" },
                            "iv": { key1: "ce_iv", key2: "pe_iv", r1: "CE IV", r2: "PE IV" },
                            "delta": { key1: "ce_delta", key2: "pe_delta", r1: "CE Delta", r2: "PE Delta" },
                            "theta": { key1: "ce_theta", key2: "pe_theta", r1: "CE Theta", r2: "PE Theta" },
                            "vega": { key1: "ce_vega", key2: "pe_vega", r1: "CE Vega", r2: "PE Vega" },
                            "gamma": { key1: "ce_gamma", key2: "pe_gamma", r1: "CE Gamma", r2: "PE Gamma" },
                            "bid": { key1: "ce_bid", key2: "pe_bid", r1: "CE Bid", r2: "PE Bid" },
                            "ask": { key1: "ce_ask", key2: "pe_ask", r1: "CE Ask", r2: "PE Ask" },
                            "ltp": { key1: "ce_ltp", key2: "pe_ltp", r1: "CE LTP", r2: "PE LTP" }
                        };
                        const cfg = metricMap[metric] || metricMap["oi"];
                        const ceVals = snapshots.map(s => s[cfg.key1] || 0);
                        const peVals = snapshots.map(s => s[cfg.key2] || 0);
                        const avgVals = ceVals.map((v, i) => (v + (peVals[i] || 0)) / 2);

                        datasets.push({ label: `Relation 1: ${cfg.r1}`, data: ceVals, borderColor: '#ef4444', borderWidth: 2, tension: 0.2, yAxisID: 'y' });
                        datasets.push({ label: `Relation 2: ${cfg.r2}`, data: peVals, borderColor: '#22c55e', borderWidth: 2, tension: 0.2, yAxisID: 'y' });
                        datasets.push({ label: `Average (${cfg.r1} & ${cfg.r2})`, data: avgVals, signalLabels: chart2Signals, borderColor: '#facc15', borderWidth: 2, pointRadius: sigPointRadii, pointHoverRadius: 5, tension: 0.2, yAxisID: 'y' });
                        datasets.push({ label: `${cfg.r1} Trend`, data: computeLinearTrend(ceVals), borderColor: '#f87171', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, yAxisID: 'y' });
                        datasets.push({ label: `${cfg.r2} Trend`, data: computeLinearTrend(peVals), borderColor: '#4ade80', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, yAxisID: 'y' });
                    }
                    historyChartObj.data.labels = times;
                    historyChartObj.data.datasets = datasets;
                    historyChartObj.update('none');
                    updateZoomBadge(historyChartObj);
                })
                .catch(err => {
                    if (err.name === 'AbortError') {
                        if (metaBadge && !hasHistoryData) metaBadge.innerText = "Sheet timeout — retrying next tick";
                    } else {
                        console.error("Historical chart error:", err);
                    }
                });
        }

        const spotLinePlugin = {
            id: 'spotLinePlugin',
            afterDraw: (chart) => {
                const index = chart.data.labels.indexOf(currentAtmStrike);
                if (index !== -1) {
                    const meta = chart.getDatasetMeta(0);
                    if (meta && meta.data[index]) {
                        const x = meta.data[index].x;
                        const { top, bottom } = chart.scales.y;
                        const ctx = chart.ctx;
                        ctx.save();
                        ctx.beginPath();
                        ctx.moveTo(x, top);
                        ctx.lineTo(x, bottom);
                        ctx.lineWidth = 3;
                        ctx.strokeStyle = '#facc15';
                        ctx.setLineDash([6, 4]);
                        ctx.stroke();
                        ctx.fillStyle = '#facc15';
                        ctx.font = 'bold 12px Segoe UI';
                        ctx.fillText('★ ATM (' + currentAtmStrike + ')', x + 5, top + 15);
                        ctx.restore();
                    }
                }
            }
        };

        function renderPageData(data) {
            if (Array.isArray(data.expiries) && data.expiries.length > 0) {
                const expSel = document.getElementById("expirySelector");
                if (expSel && expSel.options.length <= 1) {
                    data.expiries.forEach(exp => {
                        const opt = document.createElement("option");
                        opt.value = exp;
                        opt.text = exp;
                        if (exp === data.expiry) opt.selected = true;
                        expSel.appendChild(opt);
                    });
                }
            }

            const errEl = document.getElementById("errorContainer");
            if (data.error) {
                errEl.style.display = "block";
                errEl.innerText = "Option Chain Feed: " + data.error;
                if (!hasSpotData) {
                    document.getElementById("cardSpot").innerText = "Feed Offline";
                    document.getElementById("cardUpdated").innerText = data.updated || new Date().toLocaleTimeString();
                }
                return;
            }
            errEl.style.display = "none";
            hasSpotData = true;

            rawRows = data.rows || [];
            currentAtmStrike = data.atm_strike || 0;
            document.getElementById("cardSpot").innerText = `${data.spot_price || 0} (ATM: ${currentAtmStrike})`;
            document.getElementById("cardUpdated").innerText = data.updated || "";

            const pcrVal = data.pcr || 0;
            const pcrBox = document.getElementById("cardPcrBox");
            pcrBox.style.borderLeftColor = pcrVal >= 1.0 ? "#22c55e" : "#ef4444";
            document.getElementById("cardPcr").innerHTML = pcrVal >= 1.0 ? `${pcrVal} <span style="color:#22c55e; font-size:14px;">(Bullish)</span>` : `${pcrVal} <span style="color:#ef4444; font-size:14px;">(Bearish)</span>`;

            let maxCE = -1, resStrike = 0, maxPE = -1, supStrike = 0;
            rawRows.forEach(r => {
                if (r.ce_oi > maxCE) { maxCE = r.ce_oi; resStrike = r.strike; }
                if (r.pe_oi > maxPE) { maxPE = r.pe_oi; supStrike = r.strike; }
            });
            document.getElementById("maxCallStrike").innerText = resStrike;
            document.getElementById("maxPutStrike").innerText = supStrike;
            updateChartMetric();
        }

        function initChart() {
            const ctx = document.getElementById('analysisChart').getContext('2d');
            myChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: [],
                    datasets: [
                        { type: 'bar', label: 'Puts (PE)', data: [], backgroundColor: 'rgba(34, 197, 94, 0.85)', borderColor: '#22c55e', borderWidth: 1 },
                        { type: 'bar', label: 'Calls (CE)', data: [], backgroundColor: 'rgba(239, 68, 68, 0.85)', borderColor: '#ef4444', borderWidth: 1 },
                        { type: 'line', label: 'PE Trend', data: [], borderColor: '#4ade80', borderDash: [6, 4], borderWidth: 2, pointRadius: 0 },
                        { type: 'line', label: 'CE Trend', data: [], borderColor: '#f87171', borderDash: [6, 4], borderWidth: 2, pointRadius: 0 }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    interaction: { mode: 'index', intersect: false },
                    hover: { mode: 'index', intersect: false },
                    scales: {
                        x: { ticks: { color: '#94a3b8', font: { weight: 'bold' } }, grid: { color: '#334155' } },
                        y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } }
                    },
                    plugins: {
                        legend: { labels: { color: '#f8fafc', font: { size: 13 } } },
                        tooltip: buildTimeOnlyTooltipConfig('Strike')
                    }
                },
                plugins: [spotLinePlugin, hoverCrosshairPlugin]
            });
        }

        function updateChartMetric() {
            const selected = document.getElementById("metricSelector").value;
            const strikes = rawRows.map(r => r.strike);
            const map = {
                depth_qty: ["1 - Order Book Depth Quantity (Put Bid Qty vs Call Ask Qty)", "Put Bid Qty (Buy Vol)", "Call Ask Qty (Sell Vol)", r => r.pe_bid_qty, r => r.ce_ask_qty],
                oi_chg: ["Change in Open Interest (Chg in OI)", "Put Chg in OI", "Call Chg in OI", r => r.pe_oi_chg, r => r.ce_oi_chg],
                oi: ["Open Interest (PE vs CE)", "Put OI", "Call OI", r => r.pe_oi, r => r.ce_oi],
                vol: ["Trading Volume", "Put Volume", "Call Volume", r => r.pe_vol, r => r.ce_vol],
                delta: ["Option Delta Exposure", "Put Delta", "Call Delta", r => r.pe_delta, r => r.ce_delta],
                theta: ["Theta Time Decay Rate", "Put Theta", "Call Theta", r => r.pe_theta, r => r.ce_theta]
            };
            const [metricTitle, peLabel, ceLabel, peFn, ceFn] = map[selected] || map.oi_chg;
            const peData = rawRows.map(peFn), ceData = rawRows.map(ceFn);

            document.getElementById("chartHeaderTitle").innerText = metricTitle;
            myChart.data.labels = strikes;
            myChart.data.datasets[0].label = peLabel; myChart.data.datasets[0].data = peData;
            myChart.data.datasets[1].label = ceLabel; myChart.data.datasets[1].data = ceData;
            myChart.data.datasets[2].label = `${peLabel} Trend`; myChart.data.datasets[2].data = computeLinearTrend(peData);
            myChart.data.datasets[3].label = `${ceLabel} Trend`; myChart.data.datasets[3].data = computeLinearTrend(ceData);
            myChart.update('none');

            const count = rawRows.length || 1;
            const peSum = peData.reduce((a, b) => a + Math.abs(b), 0);
            const ceSum = ceData.reduce((a, b) => a + Math.abs(b), 0);
            const totalSum = peSum + ceSum;
            const pePct = totalSum > 0 ? ((peSum / totalSum) * 100).toFixed(1) : 50;
            const cePct = totalSum > 0 ? ((ceSum / totalSum) * 100).toFixed(1) : 50;

            document.getElementById("gaugeMetricName").innerText = metricTitle.toUpperCase();
            document.getElementById("gaugePeLabel").innerText = `PUT (PE) TOTAL: ${peSum.toLocaleString()} (${pePct}%)`;
            document.getElementById("gaugeCeLabel").innerText = `CALL (CE) TOTAL: ${ceSum.toLocaleString()} (${cePct}%)`;
            document.getElementById("gaugePeAvg").innerText = `Put Avg: ${(peSum / count).toFixed(2)}`;
            document.getElementById("gaugeCeAvg").innerText = `Call Avg: ${(ceSum / count).toFixed(2)}`;
            document.getElementById("gaugeRatioText").innerText = `Put/Call Ratio: ${ceSum > 0 ? (peSum / ceSum).toFixed(2) : "0.00"}`;
            document.getElementById("gaugePeBar").style.width = `${pePct}%`;
            document.getElementById("gaugeCeBar").style.width = `${cePct}%`;
        }

        function fetchOptionChainData() {
            if (dataAbortCtrl) dataAbortCtrl.abort();
            dataAbortCtrl = new AbortController();
            return fetchWithTimeout(`/api/data?symbol=${currentSymbol}&expiry=${currentExpiry}&_t=${Date.now()}`, dataAbortCtrl.signal, 7500)
                .then(r => r.json())
                .then(data => renderPageData(data))
                .catch(e => {
                    if (e.name !== 'AbortError') {
                        console.error(e);
                    } else if (!hasSpotData) {
                        document.getElementById("cardSpot").innerText = "Retrying...";
                    }
                });
        }

        let timerSeconds = 10, isSyncing = false;
        function fetchDataInBackground(isInitial = false) {
            if (isSyncing) return;
            isSyncing = true;
            document.getElementById("syncBadge").innerText = "Syncing...";

            // Hard 9-second watchdog guarantees the 10s Auto Sync loop can never get stuck
            let finished = false;
            const unlockSync = () => {
                if (finished) return;
                finished = true;
                isSyncing = false;
                timerSeconds = 10;
                document.getElementById("syncBadge").innerHTML = 'Auto Sync: <span id="countdown">' + timerSeconds + '</span>s';
            };
            const watchdogId = setTimeout(unlockSync, 9000);

            Promise.allSettled([
                fetchOptionChainData(),
                loadHistoricalChart(isInitial, true),
                loadSignalChart(isInitial, true)
            ]).finally(() => {
                clearTimeout(watchdogId);
                unlockSync();
            });
        }

        try { initChart(); } catch(e) {}
        try { initHistoryChart(); } catch(e) {}
        try { initSignalChart(); } catch(e) {}
        fetchDataInBackground(true);

        setInterval(() => {
            if (isSyncing) return;
            if (--timerSeconds <= 0) { fetchDataInBackground(false); }
            else {
                const cdEl = document.getElementById("countdown");
                if (cdEl) cdEl.innerText = timerSeconds;
            }
        }, 1000);
    </script>
</body>
</html>"""

# --- LIVE TRADE & POSITION DASHBOARD TEMPLATE ---
POSITIONS_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <title>Upstox Private Live Trade & Positions Dashboard</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #0b1329; color: #f8fafc; }
        h2 { color: #38bdf8; margin-bottom: 5px; }
        .subtitle { color: #94a3b8; font-size: 13px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
        .timer-badge { background: #0284c7; color: #ffffff; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .latency-badge { background: #0f172a; border: 1px solid #334155; color: #34d399; padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; font-family: monospace; }
        .static-ip-bar { background: #131c31; border: 1px solid #334155; border-radius: 10px; padding: 12px 18px; margin-bottom: 14px; display: none; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
        .ip-chip { background: #0f172a; border: 1px solid #334155; padding: 4px 10px; border-radius: 6px; font-family: monospace; font-size: 13px; color: #38bdf8; }
        .ip-ok { color: #34d399; font-weight: bold; font-size: 12px; }
        .ip-warn { color: #fbbf24; font-weight: bold; font-size: 12px; }
        .btn-register-ip { background-color: #2563eb; color: #ffffff; border: none; padding: 7px 14px; border-radius: 6px; font-weight: 700; font-size: 12px; cursor: pointer; }
        .condition-bar { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 14px 18px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
        .badge-mode-auto { background: #065f46; color: #34d399; padding: 5px 12px; border-radius: 6px; font-weight: bold; font-size: 13px; border: 1px solid #10b981; }
        .badge-mode-manual { background: #78350f; color: #fbbf24; padding: 5px 12px; border-radius: 6px; font-weight: bold; font-size: 13px; border: 1px solid #f59e0b; }
        .badge-pill { background: #0f172a; color: #94a3b8; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 12px; border: 1px solid #334155; }
        .badge-active-ok { background: #064e3b; color: #6ee7b7; border-color: #10b981; }
        .badge-active-full { background: #7f1d1d; color: #fca5a5; border-color: #ef4444; }
        .order-approval-box { background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border: 2px solid #f59e0b; border-radius: 12px; padding: 18px 22px; margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; gap: 20px; flex-wrap: wrap; }
        .order-details-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 12px; flex: 1; min-width: 300px; }
        .order-field { background: #0f172a; padding: 8px 12px; border-radius: 6px; border: 1px solid #334155; }
        .order-field-label { font-size: 11px; color: #94a3b8; text-transform: uppercase; font-weight: 600; }
        .order-field-val { font-size: 14px; font-weight: bold; color: #f8fafc; margin-top: 2px; }
        .order-actions-corner { display: flex; flex-direction: column; gap: 10px; min-width: 150px; }
        .btn-place-order { background-color: #22c55e; color: #052e16; border: none; padding: 12px 20px; border-radius: 8px; font-weight: 800; font-size: 14px; cursor: pointer; }
        .btn-place-order:disabled { opacity: 0.45; cursor: not-allowed; background-color: #475569; color: #94a3b8; }
        .btn-cancel-order { background-color: #ef4444; color: #ffffff; border: none; padding: 10px 20px; border-radius: 8px; font-weight: 700; font-size: 13px; cursor: pointer; }
        .btn-reset-preview { background-color: #334155; color: #e2e8f0; border: 1px solid #475569; padding: 5px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; }
        .btn-sm-edit { background-color: #2563eb; color: #ffffff; border: none; padding: 4px 10px; border-radius: 5px; font-size: 11px; font-weight: 700; cursor: pointer; margin-right: 4px; }
        .btn-sm-edit:hover { background-color: #1d4ed8; }
        .btn-sm-cancel { background-color: #dc2626; color: #ffffff; border: none; padding: 4px 10px; border-radius: 5px; font-size: 11px; font-weight: 700; cursor: pointer; }
        .btn-sm-cancel:hover { background-color: #b91c1c; }
        .toast-banner { padding: 12px 16px; border-radius: 8px; margin-bottom: 18px; font-weight: 600; font-size: 13px; display: none; }
        .toast-success { background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }
        .toast-error { background: #450a0a; color: #fca5a5; border: 1px solid #ef4444; }
        .toast-info { background: #1e3a8a; color: #93c5fd; border: 1px solid #3b82f6; }
        .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 18px; margin-bottom: 25px; }
        .card { background: #1e293b; padding: 18px; border-radius: 10px; border-left: 5px solid #3b82f6; }
        .card-title { font-size: 12px; color: #94a3b8; text-transform: uppercase; font-weight: bold; margin-bottom: 6px; }
        .card-value { font-size: 24px; font-weight: bold; }
        .pnl-green { color: #22c55e !important; border-left-color: #22c55e !important; }
        .pnl-red { color: #ef4444 !important; border-left-color: #ef4444 !important; }
        .section-title { font-size: 18px; font-weight: bold; color: #f8fafc; margin-top: 25px; margin-bottom: 12px; border-left: 4px solid #38bdf8; padding-left: 10px; }
        .table-container { overflow-x: auto; background: #1e293b; border-radius: 10px; margin-bottom: 25px; }
        table { width: 100%; border-collapse: collapse; min-width: 1050px; }
        th, td { border: 1px solid #334155; padding: 10px 14px; text-align: center; font-size: 13px; }
        th { background-color: #0f172a; color: #38bdf8; text-transform: uppercase; font-size: 12px; }
        tr:nth-child(even){ background-color: #172554; }
        .status-open { background-color: #065f46; color: #34d399; padding: 3px 8px; border-radius: 12px; font-weight: bold; font-size: 11px; }
        .status-closed { background-color: #334155; color: #94a3b8; padding: 3px 8px; border-radius: 12px; font-weight: bold; font-size: 11px; }
        .error-msg { color: #f87171; font-weight: bold; padding: 15px; background: #450a0a; border-radius: 8px; margin-bottom: 20px; }

        .modal-backdrop { position: fixed; inset: 0; background: rgba(2, 6, 23, 0.82); display: none; align-items: center; justify-content: center; z-index: 9999; }
        .modal-card { background: #1e293b; border: 2px solid #38bdf8; border-radius: 12px; padding: 22px 26px; width: 420px; max-width: 92vw; box-shadow: 0 20px 35px rgba(0,0,0,0.6); }
        .modal-title { font-size: 17px; font-weight: 800; color: #38bdf8; margin-bottom: 4px; }
        .modal-sub { font-size: 12px; color: #94a3b8; margin-bottom: 16px; }
        .modal-row { margin-bottom: 12px; }
        .modal-row label { display: block; font-size: 12px; color: #cbd5e1; font-weight: 600; margin-bottom: 4px; }
        .modal-row input, .modal-row select { width: 100%; box-sizing: border-box; padding: 9px 12px; background: #0f172a; border: 1px solid #475569; border-radius: 6px; color: #f8fafc; font-size: 14px; font-weight: 600; }
        .modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
        .btn-modal-save { background: #22c55e; color: #052e16; border: none; padding: 9px 18px; border-radius: 6px; font-weight: 800; font-size: 13px; cursor: pointer; }
        .btn-modal-close { background: #475569; color: #f8fafc; border: none; padding: 9px 16px; border-radius: 6px; font-weight: 700; font-size: 13px; cursor: pointer; }
    </style>
</head>
<body>
    <h2>🔐 Private Live Trade & Positions Dashboard</h2>
    <div class="subtitle">
        <span>High-Speed Parallel Upstox Engine (B7 Signal Exit, B8 3:20 PM Exit, Edit Open Orders & Dynamic SL/Target)</span>
        <div style="display: flex; align-items: center; gap: 8px;">
            <span class="latency-badge" id="latencyBadge">Upstox: -ms | Sheet: -ms</span>
            <span class="timer-badge" id="syncBadge">Fast Sync: <span id="countdown">3</span>s</span>
        </div>
    </div>

    <div id="staticIpBar" class="static-ip-bar">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <span style="font-weight: bold; color: #e2e8f0; font-size: 13px;">🌐 Upstox Static IP Check:</span>
            <span style="font-size: 12px; color: #94a3b8;">Server Outbound IP:</span><span id="txtOutboundIp" class="ip-chip">Checking...</span>
            <span style="font-size: 12px; color: #94a3b8;">Registered IP:</span><span id="txtRegisteredIp" class="ip-chip">Checking...</span>
            <span id="txtIpMatchStatus" class="ip-warn">Verifying...</span>
        </div>
        <button id="btnRegisterServerIp" class="btn-register-ip" onclick="registerCurrentServerIp()">⚡ Register Server IP with Upstox</button>
    </div>

    <div class="condition-bar">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <span style="font-weight: bold; color: #e2e8f0; font-size: 14px;">Mode (B16):</span>
            <span id="badgeManualCondition" class="badge-mode-manual">LOADING...</span>
            <span id="badgeWantToTrade" class="badge-pill">Want to Trade (B17): -</span>
            <span id="badgeActiveTrades" class="badge-pill badge-active-ok">Active Trades (B28): -</span>
            <span id="badgeExitCond" class="badge-pill">Exit Cond (B29): -</span>
            <span id="badgeSignalExitB7" class="badge-pill">Signal Exit (B7): -</span>
            <span id="badgeAutoExitB8" class="badge-pill">3:20 Exit (B8): -</span>
            <span id="badgeSheetSyncTime" style="color: #64748b; font-size: 12px;">Sync: -</span>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
            <span id="engineStatusText" style="font-size: 13px; color: #38bdf8; font-weight: 600;">Checking DASHBOARD sheet...</span>
            <button id="btnResetPreview" class="btn-reset-preview" style="display: none;" onclick="resetCancelledPreview()">🔄 Reload Cancelled Order</button>
        </div>
    </div>

    <div id="orderToastBanner" class="toast-banner"></div>

    <div id="manualOrderPanel" class="order-approval-box" style="display: none;">
        <div style="flex: 1;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap;">
                <span style="font-size: 16px; font-weight: 800; color: #fbbf24;">📋 Pending Manual Order Approval (DASHBOARD B16:B29)</span>
                <span id="manualGuardWarningBadge" style="display: none; background: #7f1d1d; color: #fca5a5; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: bold;">⚠️ Blocked</span>
            </div>
            <div class="order-details-grid">
                <div class="order-field"><div class="order-field-label">Symbol & Type</div><div class="order-field-val" id="moSymbolType">-</div></div>
                <div class="order-field"><div class="order-field-label">Expiry (Month → Date)</div><div class="order-field-val" id="moExpiry">-</div></div>
                <div class="order-field"><div class="order-field-label">Strike Price</div><div class="order-field-val" id="moStrike">-</div></div>
                <div class="order-field"><div class="order-field-label">Action & Qty</div><div class="order-field-val" id="moActionQty">-</div></div>
                <div class="order-field"><div class="order-field-label">Product Mode</div><div class="order-field-val" id="moProduct">-</div></div>
                <div class="order-field"><div class="order-field-label">Live Limit Price (LTP)</div><div class="order-field-val" style="color: #38bdf8;" id="moEntryPrice">-</div></div>
                <div class="order-field"><div class="order-field-label">Total Investment</div><div class="order-field-val" style="color: #facc15;" id="moTotalInvest">-</div></div>
                <div class="order-field"><div class="order-field-label">Initial Dynamic SL (B26)</div><div class="order-field-val" style="color: #f87171;" id="moStopLoss">-</div></div>
                <div class="order-field"><div class="order-field-label">Initial Dynamic Target (B27)</div><div class="order-field-val" style="color: #4ade80;" id="moTarget">-</div></div>
            </div>
        </div>
        <div class="order-actions-corner">
            <button id="btnPlaceOrder" class="btn-place-order" onclick="executeManualOrder()">✅ Place Order</button>
            <button id="btnCancelOrder" class="btn-cancel-order" onclick="cancelManualOrder()">❌ Cancel</button>
        </div>
    </div>

    <div id="errorContainer" class="error-msg" style="display: none;"></div>

    <div class="metrics-grid">
        <div class="card" id="cardTotalPnlBox"><div class="card-title">Total Overall P&L</div><div class="card-value" id="valTotalPnl">₹ 0.00</div></div>
        <div class="card" id="cardUnrealisedPnlBox"><div class="card-title">Unrealised P&L (Live Positions)</div><div class="card-value" id="valUnrealisedPnl">₹ 0.00</div></div>
        <div class="card" id="cardRealisedPnlBox"><div class="card-title">Realised P&L (Booked Today)</div><div class="card-value" id="valRealisedPnl">₹ 0.00</div></div>
    </div>

    <div class="section-title">📊 Live & Closed Positions (Dynamic Trailing SL & Target Active)</div>
    <div class="table-container">
        <table>
            <thead>
                <tr><th>Status</th><th>Script / Trading Symbol</th><th>Product</th><th>Qty</th><th>Avg Buy Price</th><th>Avg Sell Price</th><th>LTP</th><th>Peak LTP</th><th>Dynamic SL</th><th>Dynamic Target</th><th>P&L (₹)</th></tr>
            </thead>
            <tbody id="positionsBody"><tr><td colspan="11" style="color: #94a3b8;">Loading positions...</td></tr></tbody>
        </table>
    </div>

    <div class="section-title">🎯 Active & Today's GTT Orders (Edit SL / Target / Entry)</div>
    <div class="table-container">
        <table>
            <thead>
                <tr><th>GTT ID</th><th>Trading Symbol</th><th>Side</th><th>Qty</th><th>Entry Trigger</th><th>Target Price</th><th>StopLoss Price</th><th>Status</th><th>Action</th></tr>
            </thead>
            <tbody id="gttOrdersBody"><tr><td colspan="9" style="color: #94a3b8;">Loading GTT orders...</td></tr></tbody>
        </table>
    </div>

    <div class="section-title">📋 Today's Executed Trades & Open Orders Log (Edit / Cancel Open Orders)</div>
    <div class="table-container">
        <table>
            <thead>
                <tr><th>Order Time</th><th>Trading Symbol</th><th>Type</th><th>Order Type</th><th>Qty</th><th>Price / Exec</th><th>Trigger</th><th>Status</th><th>Order ID</th><th>Action</th></tr>
            </thead>
            <tbody id="ordersBody"><tr><td colspan="10" style="color: #94a3b8;">Loading order log...</td></tr></tbody>
        </table>
    </div>

    <div id="editRegularModal" class="modal-backdrop">
        <div class="modal-card">
            <div class="modal-title">✏️ Modify Open Order</div>
            <div class="modal-sub" id="editRegSubtitle">Order ID: -</div>
            <input type="hidden" id="editRegOrderId">
            <input type="hidden" id="editRegValidity" value="DAY">
            <div class="modal-row">
                <label>Order Type</label>
                <select id="editRegOrderType" onchange="onRegOrderTypeChange()">
                    <option value="LIMIT">LIMIT</option>
                    <option value="MARKET">MARKET</option>
                    <option value="SL">SL (Stop-Loss Limit)</option>
                    <option value="SL-M">SL-M (Stop-Loss Market)</option>
                </select>
            </div>
            <div class="modal-row">
                <label>Quantity</label>
                <input type="number" id="editRegQty" min="1" step="1">
            </div>
            <div class="modal-row" id="editRegPriceRow">
                <label>Limit Price (₹)</label>
                <input type="number" id="editRegPrice" min="0" step="0.05">
            </div>
            <div class="modal-row" id="editRegTriggerRow">
                <label>Trigger Price (₹) — Required for SL / SL-M</label>
                <input type="number" id="editRegTrigger" min="0" step="0.05">
            </div>
            <div class="modal-actions">
                <button class="btn-modal-close" onclick="closeEditModals()">Cancel</button>
                <button id="btnSaveRegOrder" class="btn-modal-save" onclick="submitRegularOrderModify()">💾 Update Order</button>
            </div>
        </div>
    </div>

    <div id="editGttModal" class="modal-backdrop">
        <div class="modal-card">
            <div class="modal-title">✏️ Modify GTT Order</div>
            <div class="modal-sub" id="editGttSubtitle">GTT ID: -</div>
            <input type="hidden" id="editGttOrderId">
            <div class="modal-row">
                <label>Quantity</label>
                <input type="number" id="editGttQty" min="1" step="1">
            </div>
            <div class="modal-row">
                <label>Entry Trigger Price (₹)</label>
                <input type="number" id="editGttEntry" min="0.05" step="0.05">
            </div>
            <div class="modal-row">
                <label>Target Trigger Price (₹) — Optional</label>
                <input type="number" id="editGttTarget" min="0" step="0.05" placeholder="Leave blank if none">
            </div>
            <div class="modal-row">
                <label>StopLoss Trigger Price (₹) — Optional</label>
                <input type="number" id="editGttSl" min="0" step="0.05" placeholder="Leave blank if none">
            </div>
            <div class="modal-actions">
                <button class="btn-modal-close" onclick="closeEditModals()">Cancel</button>
                <button id="btnSaveGttOrder" class="btn-modal-save" onclick="submitGttOrderModify()">💾 Update GTT</button>
            </div>
        </div>
    </div>

    <script>
        const POLL_INTERVAL_SEC = 3;
        let timerSeconds = POLL_INTERVAL_SEC, isSyncing = false, latestEvaluatedOrder = null, detectedOutboundIp = "";
        let latestOrdersList = [], latestGttList = [];

        function revealStaticIpBarIfNeeded(forceShow = false) {
            const params = new URLSearchParams(window.location.search);
            if (forceShow || params.get("show_ip") === "1") {
                document.getElementById("staticIpBar").style.display = "flex";
                checkStaticIpStatus();
            }
        }

        function showToast(msg, type = 'info') {
            const banner = document.getElementById("orderToastBanner");
            banner.className = "toast-banner toast-" + type;
            banner.innerText = msg;
            banner.style.display = "block";
        }

        function checkStaticIpStatus() {
            fetch('/api/static_ip?_t=' + Date.now(), { cache: 'no-store' })
                .then(r => r.json())
                .then(data => {
                    detectedOutboundIp = data.outbound_ip || "";
                    document.getElementById("txtOutboundIp").innerText = detectedOutboundIp || "Unknown";
                    document.getElementById("txtRegisteredIp").innerText = data.primary_ip ? (data.secondary_ip ? `${data.primary_ip} / ${data.secondary_ip}` : data.primary_ip) : "Not Configured";
                    const st = document.getElementById("txtIpMatchStatus");
                    if (data.is_matched) { st.className = "ip-ok"; st.innerText = "✅ Static IP Matched"; }
                    else { st.className = "ip-warn"; st.innerText = data.error ? ("⚠️ " + data.error) : "⚠️ IP Mismatch"; }
                });
        }

        function registerCurrentServerIp() {
            if (!detectedOutboundIp || !confirm(`Register '${detectedOutboundIp}' as Upstox Primary Static IP?`)) return;
            const btn = document.getElementById("btnRegisterServerIp");
            btn.disabled = true; btn.innerText = "Registering...";
            fetch('/api/static_ip', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ primary_ip: detectedOutboundIp }) })
                .then(r => r.json())
                .then(d => { showToast((d.success ? "✅ " : "❌ ") + d.message, d.success ? "success" : "error"); checkStaticIpStatus(); fetchPositionsInBackground(); })
                .finally(() => { btn.disabled = false; btn.innerText = "⚡ Register Server IP with Upstox"; });
        }

        function updateCardPnl(elId, cardId, val) {
            const num = parseFloat(val) || 0;
            document.getElementById(elId).innerText = `₹ ${num.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
            document.getElementById(cardId).className = num >= 0 ? "card pnl-green" : "card pnl-red";
        }

        function renderOrderEngineState(st, diag) {
            if (diag) {
                const latEl = document.getElementById("latencyBadge");
                const uMs = diag.upstox_ms || 0, gMs = diag.gsheet_ms || 0;
                latEl.innerText = `Upstox: ${uMs}ms | Sheet: ${gMs}ms`;
                latEl.style.color = (uMs > 2500 || gMs > 2500) ? "#fbbf24" : "#34d399";
            }
            if (!st) return;
            const mode = st.manual_order || "Manual";
            const modeBadge = document.getElementById("badgeManualCondition");
            modeBadge.innerText = mode.toUpperCase();
            modeBadge.className = mode.toLowerCase() === "auto" ? "badge-mode-auto" : "badge-mode-manual";

            document.getElementById("badgeWantToTrade").innerText = `Want to Trade (B17): ${st.want_to_trade || "NO"}`;
            const curAct = st.active_trade_count ?? 0, maxAct = st.max_active_trades ?? 1;
            const actBadge = document.getElementById("badgeActiveTrades");
            actBadge.innerText = `Active Trades (B28): ${curAct} / ${maxAct}`;
            actBadge.className = "badge-pill " + (curAct >= maxAct ? "badge-active-full" : "badge-active-ok");

            const exitCond = st.exit_condition || "NONE";
            const exitBadge = document.getElementById("badgeExitCond");
            exitBadge.innerText = `Exit Cond (B29): ${exitCond}`;
            exitBadge.className = "badge-pill " + ((exitCond === "BE" || exitCond === "SE") ? "badge-active-full" : "");

            const sigExitB7 = st.signal_exit_b7 || "NO";
            const b7Badge = document.getElementById("badgeSignalExitB7");
            b7Badge.innerText = `Signal Exit (B7): ${sigExitB7}`;
            b7Badge.className = "badge-pill " + (sigExitB7 === "YES" ? "badge-active-ok" : "");

            const autoExitB8 = st.auto_exit_320_b8 || "NO";
            const b8Badge = document.getElementById("badgeAutoExitB8");
            b8Badge.innerText = `3:20 Exit (B8): ${autoExitB8}`;
            b8Badge.className = "badge-pill " + (autoExitB8 === "YES" ? "badge-active-ok" : "");

            if (st.sheet_synced_at) document.getElementById("badgeSheetSyncTime").innerText = `Sync: ${st.sheet_synced_at}`;
            document.getElementById("engineStatusText").innerText = st.status_message || "";

            if (st.show_static_ip_bar) revealStaticIpBarIfNeeded(true);
            if (st.exit_execution_notice) showToast(st.exit_execution_notice, st.exit_execution_ok ? "success" : "error");
            else if (st.auto_execution_notice) showToast(st.auto_execution_notice, st.auto_execution_ok ? "success" : "error");
            else if (st.tsl_updates && st.tsl_updates.length > 0) showToast("📈 " + st.tsl_updates.join(" | "), "success");

            document.getElementById("btnResetPreview").style.display = st.is_cancelled ? "inline-block" : "none";

            const manualPanel = document.getElementById("manualOrderPanel");
            if (st.show_manual_panel && st.evaluated_order) {
                const ev = st.evaluated_order;
                latestEvaluatedOrder = ev;
                manualPanel.style.display = "flex";
                document.getElementById("moSymbolType").innerText = `${ev.symbol} (${ev.option_type})`;
                document.getElementById("moExpiry").innerText = `${ev.expiry_month} → ${ev.expiry_date}`;
                document.getElementById("moStrike").innerHTML = ev.strike_adjusted ? `${ev.selected_strike} <span style="color:#fbbf24; font-size:11px;">(Req: ${ev.requested_strike})</span>` : `${ev.selected_strike}`;
                document.getElementById("moActionQty").innerText = `${ev.transaction_type} | Qty: ${ev.quantity}`;
                document.getElementById("moProduct").innerText = ev.product_label;
                document.getElementById("moEntryPrice").innerText = `₹ ${ev.entry_price}`;
                document.getElementById("moTotalInvest").innerText = `₹ ${Number(ev.total_investment).toLocaleString('en-IN')}`;
                document.getElementById("moStopLoss").innerText = ev.sl_price != null ? `₹ ${ev.sl_price} (${ev.sl_percent}% Dynamic)` : "Blank";
                document.getElementById("moTarget").innerText = ev.target_price != null ? `₹ ${ev.target_price} (${ev.target_percent}% Dynamic)` : "Blank";

                const warnBadge = document.getElementById("manualGuardWarningBadge");
                const placeBtn = document.getElementById("btnPlaceOrder");
                if (st.guard_blocked) {
                    warnBadge.style.display = "inline-block";
                    warnBadge.innerText = "⚠️ " + (st.guard_reason || "Blocked");
                    placeBtn.disabled = true; placeBtn.innerText = "🚫 Order Blocked";
                } else {
                    warnBadge.style.display = "none";
                    placeBtn.disabled = false; placeBtn.innerText = "✅ Place Order";
                }
            } else {
                manualPanel.style.display = "none";
            }
        }

        function onRegOrderTypeChange() {
            const ot = document.getElementById("editRegOrderType").value;
            document.getElementById("editRegPriceRow").style.display = (ot === "MARKET" || ot === "SL-M") ? "none" : "block";
            document.getElementById("editRegTriggerRow").style.display = (ot === "SL" || ot === "SL-M") ? "block" : "none";
        }

        function openEditRegularModal(orderId) {
            const ord = latestOrdersList.find(o => String(o.order_id) === String(orderId));
            if (!ord) return;
            document.getElementById("editRegOrderId").value = ord.order_id;
            document.getElementById("editRegValidity").value = ord.validity || "DAY";
            document.getElementById("editRegSubtitle").innerText = `${ord.trading_symbol} (${ord.transaction_type}) | ID: ${ord.order_id}`;
            document.getElementById("editRegOrderType").value = ["LIMIT", "MARKET", "SL", "SL-M"].includes((ord.order_type || "").toUpperCase()) ? ord.order_type.toUpperCase() : "LIMIT";
            document.getElementById("editRegQty").value = ord.quantity || 1;
            document.getElementById("editRegPrice").value = ord.price || 0;
            document.getElementById("editRegTrigger").value = ord.trigger_price || 0;
            onRegOrderTypeChange();
            document.getElementById("editRegularModal").style.display = "flex";
        }

        function openEditGttModal(gttId) {
            const gtt = latestGttList.find(g => String(g.gtt_order_id) === String(gttId));
            if (!gtt) return;
            document.getElementById("editGttOrderId").value = gtt.gtt_order_id;
            document.getElementById("editGttSubtitle").innerText = `${gtt.trading_symbol || gtt.instrument_token} (${gtt.transaction_type}) | GTT: ${gtt.gtt_order_id}`;
            document.getElementById("editGttQty").value = gtt.quantity || 1;
            document.getElementById("editGttEntry").value = gtt.entry_price || "";
            document.getElementById("editGttTarget").value = gtt.target_price != null ? gtt.target_price : "";
            document.getElementById("editGttSl").value = gtt.sl_price != null ? gtt.sl_price : "";
            document.getElementById("editGttModal").style.display = "flex";
        }

        function closeEditModals() {
            document.getElementById("editRegularModal").style.display = "none";
            document.getElementById("editGttModal").style.display = "none";
        }

        function submitRegularOrderModify() {
            const btn = document.getElementById("btnSaveRegOrder");
            btn.disabled = true; btn.innerText = "Updating...";
            const payload = {
                kind: "REGULAR",
                order_id: document.getElementById("editRegOrderId").value,
                order_type: document.getElementById("editRegOrderType").value,
                quantity: parseInt(document.getElementById("editRegQty").value, 10),
                price: parseFloat(document.getElementById("editRegPrice").value || 0),
                trigger_price: parseFloat(document.getElementById("editRegTrigger").value || 0),
                validity: document.getElementById("editRegValidity").value
            };
            fetch('/api/modify_order', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
                .then(r => r.json())
                .then(d => {
                    showToast((d.success ? "✅ " : "❌ ") + d.message, d.success ? "success" : "error");
                    if (d.success) { closeEditModals(); fetchPositionsInBackground(); }
                })
                .finally(() => { btn.disabled = false; btn.innerText = "💾 Update Order"; });
        }

        function submitGttOrderModify() {
            const btn = document.getElementById("btnSaveGttOrder");
            btn.disabled = true; btn.innerText = "Updating...";
            const tgtRaw = document.getElementById("editGttTarget").value.trim();
            const slRaw = document.getElementById("editGttSl").value.trim();
            const payload = {
                kind: "GTT",
                gtt_order_id: document.getElementById("editGttOrderId").value,
                quantity: parseInt(document.getElementById("editGttQty").value, 10),
                entry_price: parseFloat(document.getElementById("editGttEntry").value || 0),
                target_price: tgtRaw !== "" ? parseFloat(tgtRaw) : 0,
                sl_price: slRaw !== "" ? parseFloat(slRaw) : 0
            };
            fetch('/api/modify_order', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
                .then(r => r.json())
                .then(d => {
                    showToast((d.success ? "✅ " : "❌ ") + d.message, d.success ? "success" : "error");
                    if (d.success) { closeEditModals(); fetchPositionsInBackground(); }
                })
                .finally(() => { btn.disabled = false; btn.innerText = "💾 Update GTT"; });
        }

        function cancelSingleOpenOrder(kind, id) {
            if (!confirm(`Cancel ${kind} order '${id}'?`)) return;
            fetch('/api/cancel_open_order', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind, id }) })
                .then(r => r.json())
                .then(d => {
                    showToast((d.success ? "✅ " : "❌ ") + d.message, d.success ? "success" : "error");
                    fetchPositionsInBackground();
                });
        }

        function executeManualOrder() {
            const btn = document.getElementById("btnPlaceOrder");
            btn.disabled = true; btn.innerText = "Placing Order...";
            fetch('/api/manual_order_action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'PLACE', evaluated_order: latestEvaluatedOrder }) })
                .then(r => r.json())
                .then(d => { if (d.show_static_ip_bar) revealStaticIpBarIfNeeded(true); showToast((d.success ? "✅ " : "❌ ") + d.message, d.success ? "success" : "error"); fetchPositionsInBackground(); })
                .finally(() => { btn.disabled = false; btn.innerText = "✅ Place Order"; });
        }

        function cancelManualOrder() {
            fetch('/api/manual_order_action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'CANCEL', signature: latestEvaluatedOrder ? latestEvaluatedOrder.signature : "" }) })
                .then(r => r.json())
                .then(d => { showToast("🚫 " + d.message, "info"); document.getElementById("manualOrderPanel").style.display = "none"; fetchPositionsInBackground(); });
        }

        function resetCancelledPreview() {
            fetch('/api/manual_order_action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'RESET' }) }).then(() => fetchPositionsInBackground());
        }

        function fetchPositionsInBackground() {
            if (isSyncing) return;
            isSyncing = true;
            document.getElementById("syncBadge").innerText = "Syncing...";

            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 7000);

            fetch('/api/positions?_t=' + Date.now(), { cache: 'no-store', signal: controller.signal })
                .then(r => r.json())
                .then(data => {
                    const errEl = document.getElementById("errorContainer");
                    if (data.error) { errEl.style.display = "block"; errEl.innerText = "Error Fetching Positions: " + data.error; }
                    else { errEl.style.display = "none"; }

                    latestOrdersList = data.orders || [];
                    latestGttList = data.gtt_orders || [];

                    renderOrderEngineState(data.order_engine, data.diagnostics);
                    updateCardPnl("valTotalPnl", "cardTotalPnlBox", data.total_pnl);
                    updateCardPnl("valUnrealisedPnl", "cardUnrealisedPnlBox", data.unrealised_pnl);
                    updateCardPnl("valRealisedPnl", "cardRealisedPnlBox", data.realised_pnl);

                    const posBody = document.getElementById("positionsBody");
                    if (data.positions && data.positions.length > 0) {
                        posBody.innerHTML = data.positions.map(p => {
                            const pnlNum = parseFloat(p.pnl) || 0;
                            const badge = p.status === 'OPEN' ? '<span class="status-open">OPEN</span>' : '<span class="status-closed">CLOSED</span>';
                            const peak = p.best_price != null ? `₹ ${p.best_price}` : '-';
                            const dSl = p.dynamic_sl != null ? `<span style="color:#f87171; font-weight:bold;">₹ ${p.dynamic_sl}</span>` : '-';
                            const dTgt = p.dynamic_target != null ? `<span style="color:#4ade80; font-weight:bold;">₹ ${p.dynamic_target}</span>` : '-';
                            return `<tr><td>${badge}</td><td><strong>${p.trading_symbol}</strong></td><td>${p.product}</td><td><strong>${p.quantity}</strong></td><td>₹ ${p.buy_price}</td><td>₹ ${p.sell_price}</td><td>₹ ${p.ltp}</td><td style="color:#facc15;">${peak}</td><td>${dSl}</td><td>${dTgt}</td><td style="font-weight:bold; color:${pnlNum >= 0 ? '#22c55e' : '#ef4444'};">₹ ${pnlNum.toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}</td></tr>`;
                        }).join("");
                    } else {
                        posBody.innerHTML = `<tr><td colspan="11" style="color: #94a3b8;">No open or closed positions found for today.</td></tr>`;
                    }

                    const gttBody = document.getElementById("gttOrdersBody");
                    if (latestGttList.length > 0) {
                        gttBody.innerHTML = latestGttList.map(g => {
                            const actions = g.is_open
                                ? `<button class="btn-sm-edit" onclick="openEditGttModal('${g.gtt_order_id}')">✏️ Edit</button><button class="btn-sm-cancel" onclick="cancelSingleOpenOrder('GTT','${g.gtt_order_id}')">❌</button>`
                                : `<span style="color:#64748b; font-size:11px;">Locked</span>`;
                            return `<tr><td style="font-size:11px; color:#94a3b8;">${g.gtt_order_id}</td><td><strong>${g.trading_symbol || g.instrument_token}</strong></td><td style="font-weight:bold; color:${g.transaction_type === 'BUY' ? '#38bdf8' : '#f43f5e'};">${g.transaction_type}</td><td>${g.quantity}</td><td>₹ ${g.entry_price || '-'}</td><td style="color:#4ade80;">${g.target_price != null ? '₹ ' + g.target_price : '-'}</td><td style="color:#f87171;">${g.sl_price != null ? '₹ ' + g.sl_price : '-'}</td><td><strong>${g.status}</strong></td><td>${actions}</td></tr>`;
                        }).join("");
                    } else {
                        gttBody.innerHTML = `<tr><td colspan="9" style="color: #94a3b8;">No active GTT orders found.</td></tr>`;
                    }

                    const ordBody = document.getElementById("ordersBody");
                    if (latestOrdersList.length > 0) {
                        ordBody.innerHTML = latestOrdersList.map(o => {
                            const actions = o.is_open
                                ? `<button class="btn-sm-edit" onclick="openEditRegularModal('${o.order_id}')">✏️ Edit</button><button class="btn-sm-cancel" onclick="cancelSingleOpenOrder('REGULAR','${o.order_id}')">❌</button>`
                                : `<span style="color:#64748b; font-size:11px;">-</span>`;
                            return `<tr><td style="font-size:12px; color:#94a3b8;">${o.order_timestamp}</td><td><strong>${o.trading_symbol}</strong></td><td style="font-weight:bold; color:${o.transaction_type === 'BUY' ? '#38bdf8' : '#f43f5e'};">${o.transaction_type}</td><td>${o.order_type || 'LIMIT'}</td><td>${o.quantity}</td><td>₹ ${o.average_price || o.price}</td><td>₹ ${o.trigger_price || 0}</td><td><strong>${o.status}</strong></td><td style="font-size:11px; color:#64748b;">${o.order_id}</td><td>${actions}</td></tr>`;
                        }).join("");
                    } else {
                        ordBody.innerHTML = `<tr><td colspan="10" style="color: #94a3b8;">No trades executed today.</td></tr>`;
                    }
                })
                .catch(e => {
                    if (e.name !== 'AbortError') console.error(e);
                })
                .finally(() => {
                    clearTimeout(timeoutId);
                    isSyncing = false;
                    timerSeconds = POLL_INTERVAL_SEC;
                    document.getElementById("syncBadge").innerHTML = 'Fast Sync: <span id="countdown">' + timerSeconds + '</span>s';
                });
        }

        revealStaticIpBarIfNeeded(false);
        fetchPositionsInBackground();
        setInterval(() => {
            if (isSyncing) return;
            if (--timerSeconds <= 0) { fetchPositionsInBackground(); }
            else { document.getElementById("countdown").innerText = timerSeconds; }
        }, 1000);
    </script>
</body>
</html>"""

# --- ROUTES ---
@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/diagnostics")
def api_diagnostics():
    return jsonify({
        "diagnostics": LAST_DIAGNOSTICS,
        "cache_sizes": {
            "expiry_entries": len(EXPIRY_CACHE),
            "chain_entries": len(CHAIN_CACHE),
            "history_entries": len(HISTORY_CACHE)
        },
        "ist_time": get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
    })

@app.route("/api/static_ip", methods=["GET", "POST"])
def api_static_ip():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        ok, msg = update_upstox_static_ip(data.get("primary_ip") or get_current_outbound_ip(), data.get("secondary_ip"))
        return jsonify({"success": ok, "message": msg})
    resp = jsonify(fetch_upstox_static_ip_status())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/modify_order", methods=["POST"])
def api_modify_order():
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind", "REGULAR")).strip().upper()

    with ORDER_EXECUTION_LOCK:
        if kind == "GTT":
            gtt_id = str(data.get("gtt_order_id", "")).strip()
            if not gtt_id:
                return jsonify({"success": False, "message": "Missing GTT Order ID."})
            _, _, gtt_orders, _, _, _, err = fetch_upstox_positions()
            if err:
                return jsonify({"success": False, "message": err})
            target_gtt = next((g for g in gtt_orders if str(g.get("gtt_order_id")) == gtt_id), None)
            if not target_gtt:
                return jsonify({"success": False, "message": f"Active GTT {gtt_id} not found."})

            new_qty = int(data.get("quantity") or target_gtt.get("quantity", 1))
            new_entry = float(data.get("entry_price") or target_gtt.get("entry_price", 0.05))
            raw_tgt = float(data.get("target_price") or 0)
            raw_sl = float(data.get("sl_price") or 0)
            new_tgt = raw_tgt if raw_tgt > 0 else None
            new_sl = raw_sl if raw_sl > 0 else None

            ok, msg = modify_upstox_gtt_sl_target(target_gtt, new_sl, new_tgt, new_entry=new_entry, new_qty=new_qty)
            if ok:
                ts = str(target_gtt.get("trading_symbol", "")).strip().upper()
                itok = str(target_gtt.get("instrument_token", "")).strip()
                tsl_state = load_dynamic_tsl_state()
                if ts in tsl_state.get("contracts", {}):
                    tsl_state["contracts"][ts]["current_sl"] = new_sl
                    tsl_state["contracts"][ts]["current_target"] = new_tgt
                if itok in tsl_state.get("by_token", {}):
                    tsl_state["by_token"][itok]["current_sl"] = new_sl
                    tsl_state["by_token"][itok]["current_target"] = new_tgt
                save_dynamic_tsl_state(tsl_state)
            return jsonify({"success": ok, "message": msg})
        else:
            order_id = str(data.get("order_id", "")).strip()
            ok, msg = modify_upstox_regular_order(
                order_id=order_id,
                quantity=data.get("quantity", 1),
                price=data.get("price", 0.0),
                trigger_price=data.get("trigger_price", 0.0),
                order_type=data.get("order_type", "LIMIT"),
                validity=data.get("validity", "DAY")
            )
            return jsonify({"success": ok, "message": msg})

@app.route("/api/cancel_open_order", methods=["POST"])
def api_cancel_open_order():
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind", "REGULAR")).strip().upper()
    oid = str(data.get("id", "")).strip()
    if not oid:
        return jsonify({"success": False, "message": "Missing Order ID."})

    token = get_valid_token()
    if not token:
        return jsonify({"success": False, "message": "Upstox token unavailable."})

    headers = {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    proxies = get_upstox_proxies()

    with ORDER_EXECUTION_LOCK:
        if kind == "GTT":
            ok, msg = cancel_upstox_gtt_order(oid, headers, proxies)
        else:
            ok, msg = cancel_upstox_regular_order(oid, headers, proxies)
        return jsonify({"success": ok, "message": msg})

@app.route("/api/manual_order_action", methods=["POST"])
def api_manual_order_action():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).upper()
    with ORDER_EXECUTION_LOCK:
        state = load_auto_order_state()
        if action == "CANCEL":
            sig = data.get("signature", "")
            if sig and sig not in state["cancelled_signatures"]:
                state["cancelled_signatures"].append(sig)
                save_auto_order_state(state)
            return jsonify({"success": True, "message": "Manual order preview cancelled."})

        if action == "RESET":
            state["cancelled_signatures"] = []
            save_auto_order_state(state)
            return jsonify({"success": True, "message": "Cancelled order preview restored."})

        if action == "PLACE":
            cfg, err = fetch_dashboard_order_config(force_fresh=True)
            if err or not cfg:
                return jsonify({"success": False, "message": err or "Could not load DASHBOARD sheet."})
            if cfg["want_to_trade"] != "YES":
                return jsonify({"success": False, "message": "Blocked: Want_to_Trade (B17) is not YES."})

            positions, orders, gtt_orders, _, _, _, pos_err = fetch_upstox_positions()
            if pos_err:
                return jsonify({"success": False, "message": f"Cannot verify positions: {pos_err}"})

            guard = evaluate_trade_guards(
                cfg["symbol"], cfg["option_type"], cfg["max_active_trades"],
                positions, orders, gtt_orders, state.get("recent_submissions", {})
            )
            if not guard["allowed"]:
                return jsonify({"success": False, "message": guard["reason"]})

            ev_order, prep_err = prepare_evaluated_order(cfg)
            if prep_err or not ev_order:
                return jsonify({"success": False, "message": prep_err or "Order evaluation failed."})

            ok, msg, is_ip_err = submit_upstox_evaluated_order(ev_order)
            if ok:
                state.setdefault("recent_submissions", {})[ev_order["duplicate_key"]] = get_ist_now().timestamp()
                save_auto_order_state(state)
            return jsonify({"success": ok, "message": msg, "show_static_ip_bar": is_ip_err})

    return jsonify({"success": False, "message": "Invalid action."}), 400

@app.route("/Live_Trade_Position")
def live_trade_position():
    return render_template_string(POSITIONS_HTML_TEMPLATE)

@app.route("/api/positions")
def api_positions():
    cycle_t0 = time.time()

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_upstox = ex.submit(fetch_upstox_positions)
        fut_sheet = ex.submit(fetch_dashboard_order_config, False)
        positions, orders, gtt_orders, total_pnl, realised_pnl, unrealised_pnl, err = fut_upstox.result()
        cfg, cfg_err = fut_sheet.result()

    now_ist = get_ist_now()
    now_ts = now_ist.timestamp()
    ist_sync_label = now_ist.strftime("%H:%M:%S IST")

    order_engine = {
        "signal_exit_b7": cfg["signal_exit_b7"] if cfg else "NO",
        "auto_exit_320_b8": cfg["auto_exit_320_b8"] if cfg else "NO",
        "manual_order": cfg["manual_order"] if cfg else "Manual",
        "want_to_trade": cfg["want_to_trade"] if cfg else "NO",
        "max_active_trades": cfg["max_active_trades"] if cfg else 1,
        "exit_condition": cfg["exit_condition"] if cfg else "",
        "sheet_synced_at": ist_sync_label,
        "active_trade_count": 0,
        "status_message": "",
        "show_manual_panel": False,
        "evaluated_order": None,
        "guard_blocked": False,
        "guard_reason": "",
        "is_cancelled": False,
        "auto_execution_notice": None,
        "auto_execution_ok": False,
        "exit_execution_notice": None,
        "exit_execution_ok": False,
        "tsl_updates": [],
        "show_static_ip_bar": False
    }

    if cfg_err or not cfg:
        order_engine["status_message"] = cfg_err or "Unable to read DASHBOARD sheet."
    else:
        with ORDER_EXECUTION_LOCK:
            state = load_auto_order_state()
            exit_cooldowns = state.setdefault("exit_cooldowns", {})

            is_after_320 = (now_ist.hour > 15) or (now_ist.hour == 15 and now_ist.minute >= 20)
            if cfg["auto_exit_320_b8"] == "YES" and is_after_320 and not err:
                last_320_ts = float(exit_cooldowns.get("AUTO_EXIT_320", 0) or 0)
                if (now_ts - last_320_ts) >= 20:
                    ex_ok, ex_msg = execute_targeted_or_full_exit(
                        positions, orders, gtt_orders,
                        target_underlying=None, target_opt_type=None, only_options=True
                    )
                    if ex_msg:
                        exit_cooldowns["AUTO_EXIT_320"] = now_ts
                        save_auto_order_state(state)
                        order_engine["exit_execution_ok"] = bool(ex_ok)
                        order_engine["exit_execution_notice"] = f"⏰ 3:20 PM AUTO-EXIT (B8=YES): {ex_msg}"
                        order_engine["status_message"] = order_engine["exit_execution_notice"]
                        positions, orders, gtt_orders, total_pnl, realised_pnl, unrealised_pnl, err = fetch_upstox_positions()

            exit_cond = cfg.get("exit_condition", "")
            if cfg["signal_exit_b7"] == "YES" and exit_cond in ("BE", "SE") and not err:
                target_opt = "CE" if exit_cond == "BE" else "PE"
                target_sym = cfg["symbol"]
                exit_key = f"EXIT_{target_sym}_{exit_cond}"
                last_ex_ts = float(exit_cooldowns.get(exit_key, 0) or 0)

                if (now_ts - last_ex_ts) >= 15:
                    ex_ok, ex_msg = execute_targeted_or_full_exit(
                        positions, orders, gtt_orders,
                        target_underlying=target_sym, target_opt_type=target_opt
                    )
                    if ex_msg:
                        exit_cooldowns[exit_key] = now_ts
                        save_auto_order_state(state)
                        order_engine["exit_execution_ok"] = bool(ex_ok)
                        order_engine["exit_execution_notice"] = f"🛑 SIGNAL EXIT ({exit_cond} -> {target_sym} {target_opt}): {ex_msg}"
                        order_engine["status_message"] = order_engine["exit_execution_notice"]
                        positions, orders, gtt_orders, total_pnl, realised_pnl, unrealised_pnl, err = fetch_upstox_positions()
                    elif not order_engine["exit_execution_notice"]:
                        order_engine["status_message"] = f"Exit Condition {exit_cond} active (B7=YES), no open {target_sym} {target_opt} position/order found."
            elif exit_cond in ("BE", "SE") and cfg["signal_exit_b7"] != "YES":
                order_engine["status_message"] = f"Exit Condition {exit_cond} in B29 ignored because Signal Exit (B7) is NO."

            if not err:
                tsl_logs = process_dynamic_sl_and_target(positions, gtt_orders, cfg.get("sl_percent"), cfg.get("target_percent"))
                if tsl_logs:
                    order_engine["tsl_updates"] = tsl_logs

            guard = evaluate_trade_guards(
                cfg["symbol"], cfg["option_type"], cfg["max_active_trades"],
                positions, orders, gtt_orders, state.get("recent_submissions", {})
            )
            order_engine["active_trade_count"] = guard["active_count"]
            order_engine["guard_blocked"] = not guard["allowed"]
            order_engine["guard_reason"] = guard["reason"]

            if cfg["want_to_trade"] != "YES":
                if not order_engine["status_message"]:
                    order_engine["status_message"] = f"Want_to_Trade (B17) is '{cfg['want_to_trade']}' — Standing by ({guard['active_count']}/{cfg['max_active_trades']} active)."
            elif cfg["auto_exit_320_b8"] == "YES" and is_after_320:
                if not order_engine["status_message"]:
                    order_engine["status_message"] = "3:20 PM Auto-Exit (B8=YES) active — New entries paused."
            elif cfg["signal_exit_b7"] == "YES" and exit_cond in ("BE", "SE") and ((exit_cond == "BE" and cfg["option_type"] == "CE") or (exit_cond == "SE" and cfg["option_type"] == "PE")):
                if not order_engine["status_message"]:
                    order_engine["status_message"] = f"Entry paused for {cfg['symbol']} {cfg['option_type']} while B7=YES and Exit Condition (B29) = {exit_cond}."
            else:
                ev_order, prep_err = prepare_evaluated_order(cfg)
                if prep_err or not ev_order:
                    order_engine["status_message"] = f"⚠️ {prep_err}"
                else:
                    sig, dup_key = ev_order["signature"], ev_order["duplicate_key"]
                    order_engine["evaluated_order"] = ev_order
                    if cfg["manual_order"] == "Auto":
                        if not guard["allowed"]:
                            order_engine["status_message"] = f"🛡️ AUTO GUARD: {guard['reason']}"
                        else:
                            ok, msg, is_ip_err = submit_upstox_evaluated_order(ev_order)
                            if ok:
                                state.setdefault("recent_submissions", {})[dup_key] = get_ist_now().timestamp()
                                save_auto_order_state(state)
                                order_engine["auto_execution_ok"] = True
                                order_engine["auto_execution_notice"] = f"⚡ AUTO-EXECUTED ({ev_order['symbol']} {ev_order['selected_strike']} {ev_order['option_type']}): {msg}"
                                order_engine["status_message"] = order_engine["auto_execution_notice"]
                                positions, orders, gtt_orders, total_pnl, realised_pnl, unrealised_pnl, err = fetch_upstox_positions()
                            else:
                                state.setdefault("recent_submissions", {})[dup_key] = get_ist_now().timestamp()
                                save_auto_order_state(state)
                                order_engine["auto_execution_ok"] = False
                                order_engine["auto_execution_notice"] = f"❌ AUTO-ORDER FAILED: {msg}"
                                order_engine["status_message"] = order_engine["auto_execution_notice"]
                                if is_ip_err:
                                    order_engine["show_static_ip_bar"] = True
                    else:
                        if sig in state.get("cancelled_signatures", []):
                            order_engine["is_cancelled"] = True
                            order_engine["status_message"] = "Manual order preview cancelled."
                        else:
                            order_engine["show_manual_panel"] = True
                            order_engine["status_message"] = (
                                f"🛡️ MANUAL GUARD: {guard['reason']}"
                                if not guard["allowed"]
                                else f"Ready for approval: {ev_order['transaction_type']} {ev_order['symbol']} {ev_order['selected_strike']} {ev_order['option_type']} @ ₹{ev_order['entry_price']}"
                            )

    LAST_DIAGNOSTICS["total_cycle_ms"] = int((time.time() - cycle_t0) * 1000)
    LAST_DIAGNOSTICS["updated_at"] = ist_sync_label

    resp = jsonify({
        "positions": positions,
        "orders": orders,
        "gtt_orders": gtt_orders,
        "total_pnl": total_pnl,
        "realised_pnl": realised_pnl,
        "unrealised_pnl": unrealised_pnl,
        "order_engine": order_engine,
        "diagnostics": LAST_DIAGNOSTICS,
        "error": err
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/history")
def api_history():
    symbol = request.args.get("symbol", "NIFTY").upper()
    timeframe = request.args.get("timeframe", "5minute")
    day_range = request.args.get("day_range", "1day")
    chart_type = request.args.get("chart_type", "history")
    force_fresh = request.args.get("force", "0") == "1"
    sym_clean = "BANKNIFTY" if "BANK" in symbol else "NIFTY"

    if chart_type == "signal":
        tf_tab_map = {
            "5minute": f"{sym_clean}_5_MIN",
            "15minute": f"{sym_clean}_15_MIN",
            "30minute": f"{sym_clean}_30_MIN",
            "60minute": f"{sym_clean}_1_HR",
            "day": f"{sym_clean}_1_HR"
        }
        target_sheet = tf_tab_map.get(timeframe, f"{sym_clean}_5_MIN")
        snapshots, used_sheet, err = fetch_history_from_gsheet(
            sym_clean, sheet_name=target_sheet, expected_kind="signal", force_fresh=force_fresh
        )
    else:
        snapshots, used_sheet, err = fetch_history_from_gsheet(
            sym_clean, sheet_name=sym_clean, expected_kind="history", force_fresh=force_fresh
        )

    if err or not snapshots:
        return jsonify({"history": [], "sheet_used": used_sheet, "error": err or "No data found."})

    range_days_map = {"1day": 1, "3day": 3, "1week": 7, "2week": 14, "1month": 30, "3month": 90}
    target_days = range_days_map.get(day_range, 1)

    unique_dates = []
    seen_dates = set()
    for s in snapshots:
        d = s.get("date_str") or (s.get("datetime", "").split(" ")[0] if " " in s.get("datetime", "") else "")
        if d and d not in seen_dates:
            seen_dates.add(d)
            unique_dates.append(d)

    if unique_dates:
        allowed_dates = set(unique_dates[-target_days:])
        filtered = [
            s for s in snapshots
            if (s.get("date_str") or (s.get("datetime", "").split(" ")[0] if " " in s.get("datetime", "") else "")) in allowed_dates
        ]
    else:
        filtered = snapshots

    if chart_type == "signal":
        res = filtered
    else:
        tf_minutes_map = {"5minute": 5, "15minute": 15, "30minute": 30, "60minute": 60, "day": 1440}
        bucket_mins = tf_minutes_map.get(timeframe, 5)

        if bucket_mins <= 5:
            res = filtered
        elif bucket_mins == 1440:
            by_day = {}
            day_order = []
            for s in filtered:
                d_key = s.get("date_str") or (s.get("datetime", "").split(" ")[0] if " " in s.get("datetime", "") else "day")
                if d_key not in by_day:
                    day_order.append(d_key)
                by_day[d_key] = dict(s)
                by_day[d_key]["formatted_time"] = d_key
            res = [by_day[d] for d in day_order]
        else:
            bucket_sec = bucket_mins * 60
            buckets = {}
            bucket_keys = []
            for idx, s in enumerate(filtered):
                ts = s.get("timestamp", 0)
                if ts > 0:
                    ist_offset = 19800
                    b_key = (ts + ist_offset) // bucket_sec
                else:
                    step = max(1, bucket_mins // 5)
                    b_key = idx // step
                if b_key not in buckets:
                    bucket_keys.append(b_key)
                buckets[b_key] = s
            res = [buckets[k] for k in bucket_keys]

    if len(res) > 1500:
        res = res[-1500:]

    resp = jsonify({
        "history": res,
        "sheet_used": used_sheet,
        "total_sheet_rows": len(snapshots),
        "error": None
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/download_history")
def download_history():
    symbol = request.args.get("symbol", "NIFTY").upper()
    snapshots, used_sheet, err = fetch_history_from_gsheet(symbol, expected_kind="history", force_fresh=True)
    if err or not snapshots:
        return Response(f"Error: {err or 'No history available'}", status=400, mimetype="text/plain")
    si = io.StringIO()
    cw = csv.writer(si)
    headers = [
        "Time", "Symbol", "Price LTP", "ATM Strike", "PCR",
        "CE OI", "PE OI", "CE Chg in OI", "PE Chg in OI",
        "CE Vol", "PE Vol", "CE IV", "PE IV",
        "CE Delta", "PE Delta", "CE Theta", "PE Theta",
        "CE Vega", "PE Vega", "CE Gamma", "PE Gamma",
        "CE Bid", "PE Bid", "CE Ask", "PE Ask",
        "CE LTP", "PE LTP", "Min ROC (Buy)", "Max ROC (Sell)",
        "Cross ROC Sig", "Main Volatility", "5 Min Signal", "15 Min Signal", "30 Min Signal", "1 Hr Signal"
    ]
    cw.writerow(headers)
    for s in snapshots:
        cw.writerow([
            s["datetime"], s["symbol"], s["spot_price"], s["atm_strike"], s["pcr"],
            s["ce_oi"], s["pe_oi"], s["ce_oi_chg"], s["pe_oi_chg"],
            s["ce_vol"], s["pe_vol"], s["ce_iv"], s["pe_iv"],
            s["ce_delta"], s["pe_delta"], s["ce_theta"], s["pe_theta"],
            s["ce_vega"], s["pe_vega"], s["ce_gamma"], s["pe_gamma"],
            s["ce_bid"], s["pe_bid"], s["ce_ask"], s["pe_ask"],
            s["ce_ltp"], s["pe_ltp"], s["min_roc"], s["max_roc"],
            s["cross_roc"], s["main_volatility"], s.get("signal_5m", ""),
            s.get("signal_15m", ""), s.get("signal_30m", ""), s.get("signal_1h", "")
        ])
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={symbol}_gsheet_history.csv"}
    )

@app.route("/api/data")
def api_data():
    symbol = request.args.get("symbol", "NIFTY").upper()
    if symbol not in INSTRUMENT_KEYS:
        symbol = "NIFTY"
    selected_expiry = request.args.get("expiry", "ALL")
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)
    resp = jsonify({
        "symbol": symbol, "spot_price": spot_price, "atm_strike": atm_strike,
        "expiry": expiry, "expiries": expiries, "updated": updated,
        "pcr": pcr, "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi,
        "rows": rows, "error": error
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/")
def index():
    symbol = request.args.get("symbol", "NIFTY").upper()
    if symbol not in INSTRUMENT_KEYS:
        symbol = "NIFTY"
    selected_expiry = request.args.get("expiry", "ALL")
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)
    return render_template_string(
        MAIN_HTML_TEMPLATE, symbol=symbol, rows=rows, spot_price=spot_price,
        atm_strike=atm_strike, expiry=expiry, expiries=expiries, updated=updated,
        pcr=pcr, total_ce_oi=total_ce_oi, total_pe_oi=total_pe_oi, error=error
    )

@app.route("/analysis")
def analysis():
    symbol = request.args.get("symbol", "NIFTY").upper()
    if symbol not in INSTRUMENT_KEYS:
        symbol = "NIFTY"
    selected_expiry = request.args.get("expiry", "ALL")
    ACTIVE_ANALYSIS_SYMBOLS[symbol] = time.time()
    return render_template_string(ANALYSIS_HTML_TEMPLATE, symbol=symbol, expiry=selected_expiry, expiries=[])

@app.route("/download")
def download_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    if symbol not in INSTRUMENT_KEYS:
        symbol = "NIFTY"
    selected_expiry = request.args.get("expiry", "ALL")
    rows, spot_price, atm_strike, expiry, _, updated, pcr, _, _, error = process_option_data(symbol, selected_expiry)
    if error:
        return Response(f"Error: {error}", status=400, mimetype="text/plain")
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["Symbol", "Expiry", "Spot Price", "ATM Strike", "PCR", "Updated (IST)"])
    cw.writerow([symbol, expiry, spot_price, atm_strike, pcr, updated])
    cw.writerow([])
    cw.writerow([
        "CE_OI", "CE_OI_Chg", "CE_Vol", "CE_IV", "CE_Delta", "CE_Theta", "CE_Vega", "CE_Gamma",
        "CE_Bid", "CE_Ask", "CE_LTP", "Strike_Price", "PE_LTP", "PE_Bid", "PE_Ask",
        "PE_Delta", "PE_Theta", "PE_Vega", "PE_Gamma", "PE_IV", "PE_Vol", "PE_OI_Chg", "PE_OI"
    ])
    for r in rows:
        cw.writerow([
            r["ce_oi"], r["ce_oi_chg"], r["ce_vol"], r["ce_iv"], r["ce_delta"], r["ce_theta"],
            r["ce_vega"], r["ce_gamma"], r["ce_bid"], r["ce_ask"], r["ce_ltp"], r["strike"],
            r["pe_ltp"], r["pe_bid"], r["pe_ask"], r["pe_delta"], r["pe_theta"], r["pe_vega"],
            r["pe_gamma"], r["pe_iv"], r["pe_vol"], r["pe_oi_chg"], r["pe_oi"]
        ])
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={symbol}_{expiry}_option_chain.csv"}
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), threaded=True)
