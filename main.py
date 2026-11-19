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

AUTH_COOLDOWN = {"last_failure_time": 0, "cooldown_duration": 1500}

# --- PERSISTENT HTTP CONNECTION POOL (ELIMINATES REPEATED SSL HANDSHAKES) ---
HTTP_SESSION = requests.Session()
_http_adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=0)
HTTP_SESSION.mount("https://", _http_adapter)
HTTP_SESSION.mount("http://", _http_adapter)

# --- IN-MEMORY FAST TTL CACHES (PREVENTS LAG & REDUCES RAM/API LOAD) ---
EXPIRY_CACHE = {}           # symbol -> {"ts": float, "expiries": list}
CHAIN_CACHE = {}            # (symbol, expiry) -> {"ts": float, "data": list}
DASHBOARD_CFG_CACHE = {"ts": 0.0, "cfg": None, "latency_ms": 0}
HISTORY_CACHE = {}          # sheet_key -> {"ts": float, "snapshots": list}
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
                timeout=8
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
            res = HTTP_SESSION.get(url, proxies=proxies, timeout=5)
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
            timeout=6
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
            timeout=8
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

def modify_upstox_gtt_sl_target(gtt_order, new_sl, new_target):
    token = get_valid_token()
    if not token:
        return False, "Upstox token unavailable."
    gtt_order_id = gtt_order.get("gtt_order_id")
    raw_rules = gtt_order.get("raw_rules") or []
    entry_rule = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "ENTRY"), None)
    if not gtt_order_id or not entry_rule:
        return False, "Missing GTT ID or ENTRY rule."

    updated_rules = [{"strategy": "ENTRY", "trigger_type": entry_rule.get("trigger_type", "IMMEDIATE"), "trigger_price": float(entry_rule.get("trigger_price", 0.05))}]
    existing_tgt = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "TARGET"), None)
    if new_target is not None:
        updated_rules.append({"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": float(new_target)})
    elif existing_tgt:
        updated_rules.append({"strategy": "TARGET", "trigger_type": existing_tgt.get("trigger_type", "IMMEDIATE"), "trigger_price": float(existing_tgt.get("trigger_price", 0.05))})

    existing_sl = next((r for r in raw_rules if str(r.get("strategy", "")).upper() == "STOPLOSS"), None)
    if new_sl is not None:
        updated_rules.append({"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": float(new_sl)})
    elif existing_sl:
        updated_rules.append({"strategy": "STOPLOSS", "trigger_type": existing_sl.get("trigger_type", "IMMEDIATE"), "trigger_price": float(existing_sl.get("trigger_price", 0.05))})

    try:
        res = HTTP_SESSION.put(
            "https://api.upstox.com/v3/order/gtt/modify",
            json={"type": gtt_order.get("type", "MULTIPLE"), "quantity": int(gtt_order.get("quantity", 1)), "gtt_order_id": gtt_order_id, "rules": updated_rules},
            headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            proxies=get_upstox_proxies(),
            timeout=8
        )
        rj = res.json() if res.content else {}
        if res.status_code == 200 and rj.get("status") == "success":
            return True, f"Updated {gtt_order_id} -> SL: {new_sl or 'N/A'} | Target: {new_target or 'N/A'}"
        err = rj.get("errors", [{}])[0].get("message") if isinstance(rj.get("errors"), list) and rj.get("errors") else res.text
        return False, f"GTT Modify Rejected ({res.status_code}): {err}"
    except Exception as e:
        return False, f"GTT Modify Exception: {e}"

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

# --- EXIT CONDITION (BE -> CE / SE -> PE) & 3:20 PM AUTO EXIT (ONLY WHEN B7 == YES) ---
def cancel_upstox_regular_order(order_id, headers, proxies):
    try:
        res = HTTP_SESSION.delete(
            f"https://api-hft.upstox.com/v3/order/cancel?order_id={urllib.parse.quote(str(order_id))}",
            headers=headers,
            proxies=proxies,
            timeout=6
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
            timeout=6
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
            timeout=8
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

    # Cancel open orders & GTTs concurrently for instant release of margin
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

    # Exit matching active positions at MARKET price concurrently
    if matching_positions:
        with ThreadPoolExecutor(max_workers=4) as ex:
            exit_futs = [ex.submit(exit_position_at_market, p, headers, proxies) for p in matching_positions]
            for fut in as_completed(exit_futs):
                ok, msg = fut.result()
                logs.append(msg)
                if not ok:
                    all_ok = False

    return all_ok, " | ".join(logs)

# --- GOOGLE SHEET DASHBOARD CONFIG (B7, B16:B29) WITH FAST MICRO-CACHE & FALLBACK ---
def fetch_dashboard_order_config(force_fresh=False):
    now = time.time()
    with CACHE_LOCK:
        if not force_fresh and DASHBOARD_CFG_CACHE["cfg"] is not None and (now - DASHBOARD_CFG_CACHE["ts"]) < 1.5:
            return dict(DASHBOARD_CFG_CACHE["cfg"]), None

    t0 = time.time()
    sheet_id = SPREADSHEET_ID or "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM"
    cb = int(now * 1000)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache"
    }
    # Narrow range A1:D35 makes Google Sheets gviz respond ~3x faster than A1:Z40
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&headers=0&sheet=DASHBOARD&range=A1:D35&_cb={cb}"

    raw_csv, last_err = None, None
    try:
        res = HTTP_SESSION.get(url, headers=headers, timeout=4.5)
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
        # Stale-while-error fallback: use last valid config if < 30s old so a brief Google blip doesn't stall trading
        with CACHE_LOCK:
            if DASHBOARD_CFG_CACHE["cfg"] is not None and (now - DASHBOARD_CFG_CACHE["ts"]) < 30.0:
                return dict(DASHBOARD_CFG_CACHE["cfg"]), None
        return None, last_err or "Could not fetch DASHBOARD sheet."

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
            ["3_20_pm_exit", "auto_exit_3_20", "exit_at_3_20", "square_off_3_20", "auto_square_off", "auto_exit", "exit_enabled", "3_20_exit", "320_pm_exit"],
            7,
            prefer_exact_row=True
        ).strip().upper()
        auto_exit_320 = "YES" if b7_raw in ("YES", "Y", "TRUE", "1") else "NO"

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
            ["exit_condition", "exit_signal", "exit", "exit_cond", "ce_pe_exit", "signal_exit", "trade_exit", "exit_type", "exit_status", "new_signal", "signal"],
            29,
            prefer_exact_row=True
        ).strip().upper()

        if exit_cond_raw not in ("BE", "SE"):
            for candidate in (txn_raw, opt_raw, want_raw):
                if candidate in ("BE", "SE"):
                    exit_cond_raw = candidate
                    break
        if exit_cond_raw not in ("BE", "SE"):
            for r_idx in range(min(len(grid), 35)):
                if len(grid[r_idx]) >= 2:
                    b_val = str(grid[r_idx][1]).strip().upper()
                    if b_val in ("BE", "SE"):
                        exit_cond_raw = b_val
                        break

        cfg = {
            "auto_exit_320": auto_exit_320,
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
        with CACHE_LOCK:
            DASHBOARD_CFG_CACHE["ts"] = time.time()
            DASHBOARD_CFG_CACHE["cfg"] = cfg
            DASHBOARD_CFG_CACHE["latency_ms"] = latency_ms
        LAST_DIAGNOSTICS["gsheet_status"] = "OK"
        return dict(cfg), None
    except Exception as e:
        LAST_DIAGNOSTICS["gsheet_status"] = f"ERR ({e})"
        return None, f"Error parsing DASHBOARD sheet: {e}"

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
            timeout=6
        )
        if res.status_code == 401:
            invalidate_session()
            new_tok = perform_single_auth(force_refresh=True)
            if new_tok:
                headers["Authorization"] = f"Bearer {new_tok}"
                res = HTTP_SESSION.get(
                    f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}",
                    headers=headers,
                    timeout=6
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
                timeout=8
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
                timeout=8
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

# --- GOOGLE SHEET HISTORICAL & SIGNAL DATA LOADER (WITH TTL CACHE) ---
def parse_timestamp(dt_str):
    if not dt_str:
        return 0
    dt_str = str(dt_str).strip()
    for fmt in (
        "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M",
        "%m/%d/%y %H:%M:%S", "%d/%m/%y %H:%M:%S", "%m/%d/%y %H:%M", "%d/%m/%y %H:%M",
        "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"
    ):
        try:
            return int(datetime.strptime(dt_str, fmt).timestamp())
        except ValueError:
            pass
    return 0

def extract_date_and_time_parts(time_raw):
    raw = str(time_raw).strip()
    if " " in raw:
        parts = raw.split()
        date_part = parts[0]
        t_parts = parts[1].split(":")
        time_part = f"{t_parts[0]}:{t_parts[1]}" if len(t_parts) >= 2 else parts[1]
        return date_part, time_part
    t_parts = raw.split(":")
    if len(t_parts) >= 2:
        return "", f"{t_parts[0]}:{t_parts[1]}"
    return raw, raw

def fetch_history_from_gsheet(symbol="NIFTY", sheet_name=None):
    cache_key = f"{symbol.upper()}|{sheet_name or ''}"
    now = time.time()
    with CACHE_LOCK:
        cached = HISTORY_CACHE.get(cache_key)
        if cached and (now - cached["ts"]) < 8.0 and cached["snapshots"]:
            return cached["snapshots"], None

    sheet_id = SPREADSHEET_ID or "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM"
    candidate_sheets = [sheet_name, sheet_name.upper(), symbol.upper(), "Sheet1", ""] if sheet_name else ["NIFTY" if "BANK" not in symbol.upper() else "BANKNIFTY", symbol.upper(), "Sheet1", ""]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    raw_csv, last_err = None, None

    for s_name in candidate_sheets:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(s_name)}" if s_name else f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        try:
            res = HTTP_SESSION.get(url, headers=headers, timeout=6)
            if res.status_code == 200:
                text = res.text.strip().lstrip('\ufeff')
                if text.startswith("<!DOCTYPE") or "<html" in text.lower():
                    last_err = "Google Sheet is private. Set sharing to 'Anyone with the link can view'."
                    continue
                if len(text) > 30:
                    raw_csv = text
                    break
            else:
                last_err = f"Google Sheet HTTP status {res.status_code}"
        except Exception as e:
            last_err = f"Network error connecting to Google Sheet: {e}"

    if not raw_csv:
        with CACHE_LOCK:
            if cache_key in HISTORY_CACHE and HISTORY_CACHE[cache_key]["snapshots"]:
                return HISTORY_CACHE[cache_key]["snapshots"], None
        return [], last_err or "Could not retrieve CSV from Google Sheet."

    try:
        lines = [l.strip().lstrip('\ufeff') for l in raw_csv.lstrip('\ufeff').splitlines() if l.strip()]
        if not lines:
            return [], "Google Sheet CSV is empty."

        header_idx = 0
        for i, l in enumerate(lines[:10]):
            ll = l.lower()
            if any(k in ll for k in ("time", "symbol", "pcr", "ltp", "min roc")):
                header_idx = i
                break

        reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
        snapshots = []
        for row in reader:
            if not row:
                continue
            norm_r = {re.sub(r'^[^\w]+', '', str(k)).strip().lower(): str(v).strip() for k, v in row.items() if k is not None}

            def safe_num(primary, alts=[], default=0.0):
                for n in [primary.lower()] + [a.lower() for a in alts]:
                    if n in norm_r:
                        try:
                            return float(norm_r[n].replace(",", "").strip())
                        except (ValueError, TypeError):
                            pass
                    ns = n.replace(" ", "")
                    for kk, kv in norm_r.items():
                        if kk.replace(" ", "") == ns:
                            try:
                                return float(kv.replace(",", "").strip())
                            except (ValueError, TypeError):
                                pass
                return default

            def safe_str(primary, alts=[]):
                for n in [primary.lower()] + [a.lower() for a in alts]:
                    if n in norm_r:
                        return norm_r[n]
                    ns = n.replace(" ", "")
                    for kk, kv in norm_r.items():
                        if kk.replace(" ", "") == ns:
                            return kv
                return ""

            time_val = str(norm_r.get("time", norm_r.get("datetime", norm_r.get("timestamp", norm_r.get("date & time", norm_r.get("date and time", ""))))))
            if not time_val or time_val.lower() in ("nan", "none", "", "0.0"):
                continue

            date_str, formatted_time = extract_date_and_time_parts(time_val)
            snapshots.append({
                "timestamp": parse_timestamp(time_val),
                "datetime": time_val,
                "date_str": date_str,
                "formatted_time": formatted_time,
                "symbol": str(norm_r.get("symbol", symbol.upper())),
                "spot_price": safe_num("Price LTP", ["spot_price", "ltp"]),
                "atm_strike": safe_num("ATM Strike", ["atm_strike"]),
                "pcr": safe_num("PCR"),
                "ce_oi": safe_num("CE OI"), "pe_oi": safe_num("PE OI"),
                "ce_oi_chg": safe_num("CE Chg in OI", ["CE Chng OI", "CE Chg OI"]),
                "pe_oi_chg": safe_num("PE Chg in OI", ["PE Chng OI", "PE Chg OI"]),
                "ce_vol": safe_num("CE Vol"), "pe_vol": safe_num("PE Vol"),
                "ce_iv": safe_num("CE IV"), "pe_iv": safe_num("PE IV"),
                "ce_delta": safe_num("CE Delta"), "pe_delta": safe_num("PE Delta"),
                "ce_theta": safe_num("CE Theta"), "pe_theta": safe_num("PE Theta"),
                "ce_vega": safe_num("CE Vega"), "pe_vega": safe_num("PE Vega"),
                "ce_gamma": safe_num("CE Gamma"), "pe_gamma": safe_num("PE Gamma"),
                "ce_bid": safe_num("CE Bid"), "pe_bid": safe_num("PE Bid"),
                "ce_ask": safe_num("CE Ask"), "pe_ask": safe_num("PE Ask"),
                "ce_ltp": safe_num("CE LTP"), "pe_ltp": safe_num("PE LTP"),
                "min_roc": safe_num("Min ROC (Buy)", ["min_roc", "min roc"]),
                "max_roc": safe_num("Max ROC (Sell)", ["max_roc", "max roc"]),
                "cross_roc": safe_num("Cross ROC Sig", ["cross_roc_sig", "cross roc"]),
                "main_volatility": safe_num("Main Volatility", ["main_vol", "volatility"]),
                "new_signal": safe_str("New Signal", ["signal", "sig"])
            })

        if not snapshots:
            return [], f"Google Sheet connected ({sheet_name or symbol}) but no valid data rows were found."
        ordered_snaps = snapshots[::-1]
        with CACHE_LOCK:
            HISTORY_CACHE[cache_key] = {"ts": time.time(), "snapshots": ordered_snaps}
        return ordered_snaps, None
    except Exception as e:
        return [], f"Parsing error reading Google Sheet CSV: {e}"

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
        return HTTP_SESSION.get(url, headers=headers, proxies=proxies, timeout=5)

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
            orders.append({
                "order_id": o.get("order_id", ""),
                "trading_symbol": ts,
                "transaction_type": o.get("transaction_type", ""),
                "quantity": o.get("quantity", 0),
                "price": o.get("price", 0.0),
                "average_price": o.get("average_price", 0.0),
                "status": o.get("status", ""),
                "order_timestamp": o.get("order_timestamp", "")
            })

        gtt_orders = []
        for g in gtt_raw:
            rules = g.get("rules", []) or []
            er = next((r for r in rules if str(r.get("strategy", "")).upper() == "ENTRY"), rules[0] if rules else {})
            itok = g.get("instrument_token", "")
            g_ts = g.get("trading_symbol") or token_to_symbol.get(itok, "")
            gtt_orders.append({
                "gtt_order_id": g.get("gtt_order_id", ""),
                "type": g.get("type", "MULTIPLE"),
                "instrument_token": itok,
                "trading_symbol": g_ts,
                "transaction_type": g.get("transaction_type", ""),
                "quantity": g.get("quantity", 0),
                "status": er.get("status") or g.get("status", ""),
                "raw_rules": rules
            })

        LAST_DIAGNOSTICS["upstox_ms"] = int((time.time() - t0) * 1000)
        LAST_DIAGNOSTICS["upstox_status"] = "OK"
        return positions, orders, gtt_orders, round(total_pnl, 2), round(realised_pnl, 2), round(unrealised_pnl, 2), None
    except Exception as e:
        LAST_DIAGNOSTICS["upstox_ms"] = int((time.time() - t0) * 1000)
        LAST_DIAGNOSTICS["upstox_status"] = f"ERR ({e})"
        return [], [], [], 0.0, 0.0, 0.0, str(e)

def fetch_single_expiry_chain(instrument_key, expiry_date, headers, use_cache=True):
    cache_key = (instrument_key, expiry_date)
    now = time.time()
    if use_cache:
        with CACHE_LOCK:
            cached = CHAIN_CACHE.get(cache_key)
            if cached and (now - cached["ts"]) < 2.5 and cached["data"]:
                return expiry_date, cached["data"]
    try:
        res = HTTP_SESSION.get(
            f"https://api.upstox.com/v2/option/chain?instrument_key={urllib.parse.quote(instrument_key)}&expiry_date={expiry_date}",
            headers=headers,
            timeout=5
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
        return None, None, [], "Authentication paused to protect account from lockout."
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

# --- ANALYSIS DASHBOARD TEMPLATE ---
ANALYSIS_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <title>{{ symbol }} Option Chain & Technical Chart Analysis</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #0f172a; color: #f8fafc; }
        h2 { color: #38bdf8; margin-bottom: 5px; }
        .subtitle { color: #94a3b8; font-size: 14px; margin-bottom: 20px; }
        .nav-bar { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; align-items: center; background: #1e293b; padding: 15px; border-radius: 10px; }
        .btn { padding: 9px 18px; color: #fff; background-color: #3b82f6; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 14px; border: none; cursor: pointer; }
        .btn-table { background-color: #64748b; }
        .btn-dl { background-color: #10b981; font-size: 12px; padding: 6px 12px; text-decoration: none; color: #fff; border-radius: 6px; font-weight: bold; }
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
        .chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; flex-wrap: wrap; gap: 10px; }
        .chart-title { font-size: 18px; font-weight: bold; color: #f8fafc; }
        .timer-badge { background: #0284c7; color: #ffffff; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .error-msg { color: #f87171; font-weight: bold; padding: 15px; background: #450a0a; border-radius: 8px; }
    </style>
</head>
<body>
    <h2>📊 {{ symbol }} Advanced Analytics & Google Sheet Memory Dashboard</h2>
    <div class="subtitle">Live background auto-sync — Real-time Google Sheet snapshots & strike positioning</div>

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
        <div class="card" style="border-left-color: #facc15;"><div class="card-title">Spot Price / ATM</div><div class="card-value" id="cardSpot" style="color: #facc15;">{{ spot_price }} (ATM: {{ atm_strike }})</div></div>
        <div class="card" id="cardPcrBox"><div class="card-title">Put-Call Ratio (PCR)</div><div class="card-value" id="cardPcr">{{ pcr }}</div></div>
        <div class="card" style="border-left-color: #ef4444;"><div class="card-title">Major Resistance (Max Call OI)</div><div class="card-value" id="maxCallStrike" style="color: #ef4444;">-</div></div>
        <div class="card" style="border-left-color: #22c55e;"><div class="card-title">Major Support (Max Put OI)</div><div class="card-value" id="maxPutStrike" style="color: #22c55e;">-</div></div>
        <div class="card" style="border-left-color: #a855f7;"><div class="card-title">Last Updated (IST)</div><div class="card-value" id="cardUpdated" style="font-size: 15px;">{{ updated }}</div></div>
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
            <span class="chart-title">2 - 📈 Historical Snapshot Trend (Google Sheet Memory)</span>
            <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
                <label style="color: #94a3b8; font-weight: bold;">Head of Dropdown:</label>
                <select id="historyMetricSelector" onchange="loadHistoricalChart(true)">
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
                <select id="historyDayRangeSelector" onchange="loadHistoricalChart(true)">
                    <option value="1day" selected>1 Day</option><option value="3day">3 Day</option><option value="1week">1 Week</option>
                    <option value="2week">2 Week</option><option value="1month">1 Month</option><option value="3month">3 Month</option>
                </select>
                <label style="color: #94a3b8; font-weight: bold;">Interval:</label>
                <select id="historyTimeframeSelector" onchange="loadHistoricalChart(true)">
                    <option value="5minute" selected>5 Min</option><option value="15minute">15 Min</option><option value="30minute">30 Min</option><option value="60minute">1 Hour</option><option value="day">1 Day</option>
                </select>
                <a id="downloadHistoryBtn" href="/download_history?symbol={{ symbol }}" class="btn-dl">📥 Download Google Sheet Memory CSV</a>
            </div>
        </div>
        <canvas id="historyChart" style="max-height: 420px;"></canvas>
    </div>

    <div class="chart-box">
        <div class="chart-header">
            <span class="chart-title">3 - 🚀 Main Signal (B / BE / S / SE)</span>
            <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
                <label style="color: #94a3b8; font-weight: bold;">Data Day Range:</label>
                <select id="signalDayRangeSelector" onchange="loadSignalChart(true)">
                    <option value="1day" selected>1 Day</option><option value="3day">3 Day</option><option value="1week">1 Week</option>
                    <option value="2week">2 Week</option><option value="1month">1 Month</option><option value="3month">3 Month</option>
                </select>
                <label style="color: #94a3b8; font-weight: bold;">Interval:</label>
                <select id="signalTimeframeSelector" onchange="loadSignalChart(true)">
                    <option value="5minute" selected>5 Min</option><option value="15minute">15 Min</option><option value="30minute">30 Min</option><option value="60minute">1 Hour</option>
                </select>
            </div>
        </div>
        <canvas id="signalChart" style="max-height: 420px;"></canvas>
    </div>

    <script>
        let currentSymbol = "{{ symbol }}";
        let currentExpiry = "{{ expiry }}";
        let rawRows = {{ rows | tojson }};
        let currentAtmStrike = {{ atm_strike }};
        let myChart = null, historyChartObj = null, signalChartObj = null;
        let hasHistoryData = false, hasSignalData = false;

        const DATE_PALETTE = ['#38bdf8', '#facc15', '#4ade80', '#f472b6', '#a78bfa', '#fb923c', '#2dd4bf'];

        function buildDateMeta(snapshots) {
            const dates = [], colors = [], groups = [];
            const dateColorMap = {};
            let paletteIdx = 0;

            snapshots.forEach((s, i) => {
                let dStr = (s.date_str || "").trim();
                if (!dStr && s.datetime && String(s.datetime).includes(" ")) {
                    dStr = String(s.datetime).split(" ")[0].trim();
                }
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
            return { dates, colors, groups };
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
                groups.forEach((g, idx) => {
                    const xStart = idx === 0 ? left : (xScale.getPixelForValue(g.start - 1) + xScale.getPixelForValue(g.start)) / 2;
                    const xEnd = idx === groups.length - 1 ? right : (xScale.getPixelForValue(g.end) + xScale.getPixelForValue(g.end + 1)) / 2;
                    ctx.fillStyle = idx % 2 === 0 ? 'rgba(56, 189, 248, 0.04)' : 'rgba(250, 204, 21, 0.05)';
                    ctx.fillRect(xStart, top, xEnd - xStart, bottom - top);

                    if (idx > 0) {
                        ctx.beginPath();
                        ctx.moveTo(xStart, top);
                        ctx.lineTo(xStart, bottom);
                        ctx.lineWidth = 1.5;
                        ctx.strokeStyle = 'rgba(148, 163, 184, 0.45)';
                        ctx.setLineDash([4, 4]);
                        ctx.stroke();
                        ctx.setLineDash([]);
                    }

                    if (g.date && g.date !== "Session" && (xEnd - xStart) > 48) {
                        ctx.fillStyle = g.color;
                        ctx.font = 'bold 11px Segoe UI';
                        ctx.textAlign = 'left';
                        ctx.textBaseline = 'top';
                        ctx.fillText(g.date, xStart + 6, top + 6);
                    }
                });
                ctx.restore();
            }
        };

        const signalLabelPlugin = {
            id: 'signalLabelPlugin',
            afterDatasetsDraw: (chart) => {
                const { ctx } = chart;
                chart.data.datasets.forEach((dataset, datasetIndex) => {
                    if (dataset.label === 'Main Volatility') {
                        const meta = chart.getDatasetMeta(datasetIndex);
                        const signals = dataset.signalLabels || [];
                        meta.data.forEach((element, index) => {
                            const rawSig = (signals[index] || "").trim().toUpperCase();
                            if (rawSig === 'B' || rawSig === 'BE' || rawSig === 'S' || rawSig === 'SE') {
                                const { x, y } = element.tooltipPosition();
                                const badgeColor = rawSig === 'B' ? '#22c55e' : (rawSig === 'BE' ? '#f59e0b' : (rawSig === 'S' ? '#ef4444' : '#38bdf8'));
                                ctx.save();
                                ctx.font = 'bold 11px Segoe UI';
                                const textW = ctx.measureText(rawSig).width;
                                const padX = 5, boxH = 16, boxW = textW + padX * 2;
                                const boxX = x - boxW / 2, boxY = y - boxH - 6;

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
            fetchDataInBackground();
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
                    interaction: { mode: null },
                    hover: { mode: null },
                    events: ['click'],
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
                        tooltip: { enabled: false }
                    }
                },
                plugins: [dateBandPlugin, zeroLinePlugin, signalLabelPlugin]
            });
            loadSignalChart(true);
        }

        function loadSignalChart(isManualChange = false) {
            const dayRange = document.getElementById('signalDayRangeSelector').value;
            const timeframe = document.getElementById('signalTimeframeSelector').value;
            fetch(`/api/history?symbol=${currentSymbol}&timeframe=${timeframe}&day_range=${dayRange}&chart_type=signal`)
                .then(res => res.json())
                .then(data => {
                    if (data.error || !(data.history && data.history.length)) {
                        if (isManualChange || !hasSignalData) {
                            signalChartObj._dateColors = [];
                            signalChartObj._dateGroups = [];
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

                    const times = snapshots.map(s => s.formatted_time || s.datetime);
                    signalChartObj.data.labels = times;
                    signalChartObj.data.datasets = [
                        { label: 'Min ROC (Buy)', data: snapshots.map(s => s.min_roc || 0), borderColor: '#22c55e', borderWidth: 2, pointRadius: 0, pointHoverRadius: 0, tension: 0.2 },
                        { label: 'Max ROC (Sell)', data: snapshots.map(s => s.max_roc || 0), borderColor: '#ef4444', borderWidth: 2, pointRadius: 0, pointHoverRadius: 0, tension: 0.2 },
                        { label: 'Cross ROC Sig', data: snapshots.map(s => s.cross_roc || 0), borderColor: '#facc15', borderWidth: 2, pointRadius: 0, pointHoverRadius: 0, tension: 0.2 },
                        { label: 'Main Volatility', data: snapshots.map(s => s.main_volatility || 0), signalLabels: snapshots.map(s => s.new_signal || ""), borderColor: '#38bdf8', borderWidth: 3, pointRadius: 3, pointHoverRadius: 0, tension: 0.2 }
                    ];
                    signalChartObj.update('none');
                })
                .catch(err => console.error("Signal chart error:", err));
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
                        tooltip: { mode: 'index', intersect: false }
                    }
                },
                plugins: [dateBandPlugin]
            });
            loadHistoricalChart(true);
        }

        function loadHistoricalChart(isManualChange = false) {
            const metric = document.getElementById('historyMetricSelector').value;
            const dayRange = document.getElementById('historyDayRangeSelector').value;
            const timeframe = document.getElementById('historyTimeframeSelector').value;

            fetch(`/api/history?symbol=${currentSymbol}&timeframe=${timeframe}&metric=${metric}&day_range=${dayRange}&chart_type=history`)
                .then(res => res.json())
                .then(data => {
                    if (data.error || !(data.history && data.history.length)) {
                        if (isManualChange || !hasHistoryData) {
                            historyChartObj._dateColors = [];
                            historyChartObj._dateGroups = [];
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

                    const times = snapshots.map(s => s.formatted_time || s.datetime);
                    let datasets = [];

                    if (metric === "price_pcr") {
                        historyChartObj.options.scales.y1.display = true;
                        const spotVals = snapshots.map(s => s.spot_price);
                        const pcrVals = snapshots.map(s => s.pcr);
                        datasets.push({ label: 'Relation 2: PCR', data: pcrVals, borderColor: '#06b6d4', borderWidth: 2, tension: 0.2, yAxisID: 'y' });
                        datasets.push({ label: 'Relation 1: Price LTP', data: spotVals, borderColor: '#facc15', backgroundColor: 'rgba(250, 204, 21, 0.1)', borderWidth: 2, tension: 0.2, yAxisID: 'y1', fill: true });
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
                        datasets.push({ label: `Average (${cfg.r1} & ${cfg.r2})`, data: avgVals, borderColor: '#facc15', borderWidth: 2, tension: 0.2, yAxisID: 'y' });
                        datasets.push({ label: `${cfg.r1} Trend`, data: computeLinearTrend(ceVals), borderColor: '#f87171', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, yAxisID: 'y' });
                        datasets.push({ label: `${cfg.r2} Trend`, data: computeLinearTrend(peVals), borderColor: '#4ade80', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, yAxisID: 'y' });
                    }
                    historyChartObj.data.labels = times;
                    historyChartObj.data.datasets = datasets;
                    historyChartObj.update('none');
                })
                .catch(err => console.error("Historical chart error:", err));
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
            const errEl = document.getElementById("errorContainer");
            if (data.error) { errEl.style.display = "block"; errEl.innerText = "Error: " + data.error; return; }
            errEl.style.display = "none";

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
                    labels: rawRows.map(r => r.strike),
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
                    scales: {
                        x: { ticks: { color: '#94a3b8', font: { weight: 'bold' } }, grid: { color: '#334155' } },
                        y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } }
                    },
                    plugins: { legend: { labels: { color: '#f8fafc', font: { size: 13 } } }, tooltip: { mode: 'index', intersect: false } }
                },
                plugins: [spotLinePlugin]
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

        let timerSeconds = 10, isSyncing = false;
        function fetchDataInBackground() {
            if (isSyncing) return;
            isSyncing = true;
            document.getElementById("syncBadge").innerText = "Syncing...";
            fetch(`/api/data?symbol=${currentSymbol}&expiry=${currentExpiry}`)
                .then(r => r.json())
                .then(data => { renderPageData(data); loadHistoricalChart(false); loadSignalChart(false); })
                .catch(e => console.error(e))
                .finally(() => { isSyncing = false; document.getElementById("syncBadge").innerHTML = 'Auto Sync: <span id="countdown">' + timerSeconds + '</span>s'; });
        }

        try { initChart(); } catch(e) {}
        try { initHistoryChart(); } catch(e) {}
        try { initSignalChart(); } catch(e) {}
        renderPageData({ rows: rawRows, spot_price: {{ spot_price }}, atm_strike: {{ atm_strike }}, expiry: "{{ expiry }}", updated: "{{ updated }}", pcr: {{ pcr }} });

        setInterval(() => {
            if (isSyncing) return;
            if (--timerSeconds <= 0) { timerSeconds = 10; fetchDataInBackground(); }
            document.getElementById("countdown").innerText = timerSeconds;
        }, 1000);
    </script>
</body>
</html>"""

# --- LIVE TRADE & POSITION DASHBOARD TEMPLATE (3-SECOND FAST POLLING + LATENCY MONITOR) ---
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
    </style>
</head>
<body>
    <h2>🔐 Private Live Trade & Positions Dashboard</h2>
    <div class="subtitle">
        <span>High-Speed Parallel Upstox Engine (3s Signal Sync, 1-Hr Cooldown, B7=YES Auto-Exit & Dynamic SL/Target)</span>
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
            <span id="badgeAutoExit320" class="badge-pill">Auto Exit (B7): -</span>
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

    <div class="section-title">📋 Today's Executed Trades & Orders Log</div>
    <div class="table-container">
        <table>
            <thead>
                <tr><th>Order Time</th><th>Trading Symbol</th><th>Type</th><th>Qty</th><th>Executed Price</th><th>Status</th><th>Order ID</th></tr>
            </thead>
            <tbody id="ordersBody"><tr><td colspan="7" style="color: #94a3b8;">Loading order log...</td></tr></tbody>
        </table>
    </div>

    <script>
        const POLL_INTERVAL_SEC = 3;
        let timerSeconds = POLL_INTERVAL_SEC, isSyncing = false, latestEvaluatedOrder = null, detectedOutboundIp = "";

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

            const auto320 = st.auto_exit_320 || "NO";
            const b7Badge = document.getElementById("badgeAutoExit320");
            b7Badge.innerText = `Auto Exit (B7): ${auto320}`;
            b7Badge.className = "badge-pill " + (auto320 === "YES" ? "badge-active-ok" : "");

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

                    const ordBody = document.getElementById("ordersBody");
                    if (data.orders && data.orders.length > 0) {
                        ordBody.innerHTML = data.orders.map(o => `<tr><td style="font-size:12px; color:#94a3b8;">${o.order_timestamp}</td><td><strong>${o.trading_symbol}</strong></td><td style="font-weight:bold; color:${o.transaction_type === 'BUY' ? '#38bdf8' : '#f43f5e'};">${o.transaction_type}</td><td>${o.quantity}</td><td>₹ ${o.average_price || o.price}</td><td><strong>${o.status}</strong></td><td style="font-size:11px; color:#64748b;">${o.order_id}</td></tr>`).join("");
                    } else {
                        ordBody.innerHTML = `<tr><td colspan="7" style="color: #94a3b8;">No trades executed today.</td></tr>`;
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

@app.route("/api/manual_order_action", methods=["POST"])
def api_manual_order_action():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).upper()

    if action == "CANCEL":
        with ORDER_EXECUTION_LOCK:
            state = load_auto_order_state()
            sig = data.get("signature", "")
            if sig and sig not in state.get("cancelled_signatures", []):
                state.setdefault("cancelled_signatures", []).append(sig)
            save_auto_order_state(state)
        return jsonify({"success": True, "message": "Manual order preview cancelled."})

    if action == "RESET":
        with ORDER_EXECUTION_LOCK:
            state = load_auto_order_state()
            state["cancelled_signatures"] = []
            save_auto_order_state(state)
        return jsonify({"success": True, "message": "Cancelled state cleared."})

    if action == "PLACE":
        # Fetch sheet & positions OUTSIDE lock for instant responsiveness
        cfg, cfg_err = fetch_dashboard_order_config(force_fresh=True)
        if cfg_err or not cfg:
            return jsonify({"success": False, "message": cfg_err or "Could not read DASHBOARD sheet."})

        if cfg["exit_condition"] in ("BE", "SE"):
            return jsonify({"success": False, "message": f"Blocked: Exit Condition '{cfg['exit_condition']}' is active in DASHBOARD."})

        positions, orders, gtt_orders, _, _, _, _ = fetch_upstox_positions()
        evaluated, eval_err = prepare_evaluated_order(cfg)
        if eval_err or not evaluated:
            return jsonify({"success": False, "message": eval_err or "Order evaluation failed."})

        with ORDER_EXECUTION_LOCK:
            state = load_auto_order_state()
            guard = evaluate_trade_guards(
                cfg["symbol"],
                cfg["option_type"],
                cfg["max_active_trades"],
                positions,
                orders,
                gtt_orders,
                recent_submissions=state.get("recent_submissions", {})
            )
            if not guard["allowed"]:
                return jsonify({"success": False, "message": guard["reason"]})

            ok, msg, is_ip_err = submit_upstox_evaluated_order(evaluated)
            if ok:
                state.setdefault("recent_submissions", {})[evaluated["duplicate_key"]] = get_ist_now().timestamp()
                save_auto_order_state(state)
            return jsonify({"success": ok, "message": msg, "show_static_ip_bar": is_ip_err, "evaluated_order": evaluated})

    return jsonify({"success": False, "message": "Invalid action."})

@app.route("/api/positions")
def api_positions():
    cycle_t0 = time.time()
    now_ist = get_ist_now()

    # Fetch Upstox portfolio & Google Sheet DASHBOARD config in PARALLEL without holding any lock!
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_upstox = ex.submit(fetch_upstox_positions)
        fut_sheet = ex.submit(fetch_dashboard_order_config, False)
        positions, orders, gtt_orders, total_pnl, realised_pnl, unrealised_pnl, error = fut_upstox.result()
        cfg, cfg_err = fut_sheet.result()

    payload = {
        "manual_order": "Manual",
        "want_to_trade": "NO",
        "amount_to_invest": 0,
        "max_active_trades": 1,
        "active_trade_count": 0,
        "exit_condition": "NONE",
        "auto_exit_320": "NO",
        "sheet_synced_at": now_ist.strftime("%H:%M:%S IST"),
        "status_message": "Initializing...",
        "show_manual_panel": False,
        "evaluated_order": None,
        "guard_blocked": False,
        "guard_reason": None,
        "is_cancelled": False,
        "show_static_ip_bar": False,
        "auto_execution_notice": None,
        "auto_execution_ok": False,
        "exit_execution_notice": None,
        "exit_execution_ok": False,
        "tsl_updates": []
    }

    if cfg_err or not cfg:
        payload["status_message"] = cfg_err or "Unable to read DASHBOARD sheet."
    else:
        payload["manual_order"] = cfg["manual_order"]
        payload["want_to_trade"] = cfg["want_to_trade"]
        payload["amount_to_invest"] = cfg["amount_to_invest"]
        payload["max_active_trades"] = cfg["max_active_trades"]
        payload["exit_condition"] = cfg["exit_condition"] or "NONE"
        payload["auto_exit_320"] = cfg["auto_exit_320"]

        b7_enabled = (cfg["auto_exit_320"] == "YES")
        is_after_320_pm = (now_ist.hour > 15) or (now_ist.hour == 15 and now_ist.minute >= 20)
        block_new_entries = False

        state = load_auto_order_state()
        cooldowns = state.get("exit_cooldowns", {})

        # --- CONDITION 4: 3:20 PM IST FULL POSITION & ORDER EXIT (ONLY IF CELL B7 == "YES") ---
        if is_after_320_pm and b7_enabled:
            block_new_entries = True
            if time.time() - cooldowns.get("320_PM_EXIT", 0) >= 15:
                with ORDER_EXECUTION_LOCK:
                    state = load_auto_order_state()
                    cooldowns = state.setdefault("exit_cooldowns", {})
                    if time.time() - cooldowns.get("320_PM_EXIT", 0) >= 15:
                        ex_ok, ex_msg = execute_targeted_or_full_exit(
                            positions, orders, gtt_orders,
                            target_underlying=None, target_opt_type=None, only_options=False
                        )
                        if ex_msg:
                            cooldowns["320_PM_EXIT"] = time.time()
                            save_auto_order_state(state)
                            payload["exit_execution_notice"] = f"⏰ 3:20 PM AUTO EXIT (B7=YES): {ex_msg}"
                            payload["exit_execution_ok"] = bool(ex_ok)
                            positions, orders, gtt_orders, total_pnl, realised_pnl, unrealised_pnl, _ = fetch_upstox_positions()
            payload["status_message"] = "3:20 PM IST Square-Off Active (B7=YES) — All active positions & open orders exited."

        # --- CONDITION 3: EXIT CONDITION BE -> CE / SE -> PE (ONLY EXECUTES EXIT IF CELL B7 == "YES") ---
        exit_cond = cfg["exit_condition"]
        if exit_cond in ("BE", "SE"):
            block_new_entries = True
            target_opt = "CE" if exit_cond == "BE" else "PE"

            if b7_enabled:
                cd_key = f"EXIT_{cfg['symbol']}_{exit_cond}_{target_opt}"
                if time.time() - cooldowns.get(cd_key, 0) >= 15:
                    with ORDER_EXECUTION_LOCK:
                        state = load_auto_order_state()
                        cooldowns = state.setdefault("exit_cooldowns", {})
                        if time.time() - cooldowns.get(cd_key, 0) >= 15:
                            ex_ok, ex_msg = execute_targeted_or_full_exit(
                                positions, orders, gtt_orders,
                                target_underlying=cfg["symbol"],
                                target_opt_type=target_opt,
                                only_options=True
                            )
                            if ex_msg:
                                cooldowns[cd_key] = time.time()
                                save_auto_order_state(state)
                                payload["exit_execution_notice"] = f"🚪 EXIT CONDITION ({exit_cond} → {cfg['symbol']} {target_opt} @ MARKET): {ex_msg}"
                                payload["exit_execution_ok"] = bool(ex_ok)
                                positions, orders, gtt_orders, total_pnl, realised_pnl, unrealised_pnl, _ = fetch_upstox_positions()
                if not payload["exit_execution_notice"]:
                    payload["status_message"] = f"Exit Condition {exit_cond} ({cfg['symbol']} {target_opt}) active (B7=YES) — Open {target_opt} orders & positions exited."
            else:
                payload["status_message"] = f"Exit Signal {exit_cond} ({cfg['symbol']} {target_opt}) detected, but B7 is '{cfg['auto_exit_320']}' — Auto-Exit avoided & new Buy blocked."

        # --- DYNAMIC TRAILING SL & TARGET ON REMAINING OPEN POSITIONS ---
        if any((p.get("quantity", 0) or 0) != 0 for p in positions):
            payload["tsl_updates"] = process_dynamic_sl_and_target(
                positions=positions,
                gtt_orders=gtt_orders,
                default_sl_pct=cfg["sl_percent"],
                default_target_pct=cfg["target_percent"]
            )

        # --- CHECK GUARDS FIRST BEFORE FETCHING OPTION CHAIN (AVOIDS UNNECESSARY API CALLS) ---
        state = load_auto_order_state()
        guard = evaluate_trade_guards(
            cfg["symbol"],
            cfg["option_type"],
            cfg["max_active_trades"],
            positions,
            orders,
            gtt_orders,
            recent_submissions=state.get("recent_submissions", {})
        )
        payload["active_trade_count"] = guard["active_count"]
        payload["guard_blocked"] = not guard["allowed"]
        payload["guard_reason"] = guard["reason"] if not guard["allowed"] else None

        if not block_new_entries:
            if cfg["want_to_trade"] != "YES":
                payload["status_message"] = "Trade Avoided — Want to Trade (B17) is not YES."
            elif cfg["manual_order"].lower() == "auto" and not guard["allowed"]:
                # Skip fetching option chain completely when Auto mode is blocked by cooldown/active trade
                payload["status_message"] = f"Skipped: {guard['reason']}"
            else:
                evaluated, eval_err = prepare_evaluated_order(cfg)
                if eval_err or not evaluated:
                    payload["status_message"] = eval_err or "Order evaluation skipped."
                else:
                    payload["evaluated_order"] = evaluated
                    if cfg["manual_order"].lower() == "auto":
                        with ORDER_EXECUTION_LOCK:
                            state = load_auto_order_state()
                            recheck = evaluate_trade_guards(
                                cfg["symbol"], cfg["option_type"], cfg["max_active_trades"],
                                positions, orders, gtt_orders, recent_submissions=state.get("recent_submissions", {})
                            )
                            if not recheck["allowed"]:
                                payload["status_message"] = f"Skipped: {recheck['reason']}"
                            else:
                                ok, exec_msg, is_ip_err = submit_upstox_evaluated_order(evaluated)
                                state.setdefault("recent_submissions", {})[evaluated["duplicate_key"]] = now_ist.timestamp()
                                save_auto_order_state(state)
                                payload["show_static_ip_bar"] = is_ip_err
                                payload["status_message"] = f"Auto Mode: {exec_msg}"
                                payload["auto_execution_notice"] = f"AUTO ORDER: {exec_msg}"
                                payload["auto_execution_ok"] = ok
                    else:
                        is_cancelled = evaluated["signature"] in state.get("cancelled_signatures", [])
                        payload["is_cancelled"] = is_cancelled
                        if is_cancelled:
                            payload["show_manual_panel"] = False
                            payload["status_message"] = "Manual Order Cancelled by User (Click Reload to restore)."
                        else:
                            payload["show_manual_panel"] = True
                            payload["status_message"] = f"Manual Order Blocked: {guard['reason']}" if not guard["allowed"] else f"Awaiting Manual Approval for {evaluated['symbol']} {evaluated['selected_strike']} {evaluated['option_type']} @ ₹{evaluated['entry_price']}"

    LAST_DIAGNOSTICS["total_cycle_ms"] = int((time.time() - cycle_t0) * 1000)
    LAST_DIAGNOSTICS["updated_at"] = now_ist.strftime("%H:%M:%S IST")

    resp = jsonify({
        "positions": positions,
        "orders": orders,
        "total_pnl": total_pnl,
        "realised_pnl": realised_pnl,
        "unrealised_pnl": unrealised_pnl,
        "order_engine": payload,
        "diagnostics": dict(LAST_DIAGNOSTICS),
        "error": error
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/Live_Trade_Position", methods=["GET"])
def live_trade_position():
    return render_template_string(POSITIONS_HTML_TEMPLATE)

@app.route("/api/history")
def api_history():
    symbol = request.args.get("symbol", "NIFTY").upper()
    timeframe = request.args.get("timeframe", "5minute")
    day_range = request.args.get("day_range", "1day").lower()
    chart_type = request.args.get("chart_type", "history").lower()
    sym_clean = "BANKNIFTY" if "BANK" in symbol else "NIFTY"

    if chart_type == "signal":
        tf = timeframe.lower()
        tf_str = "15_MIN" if "15" in tf else ("30_MIN" if "30" in tf else ("1_HR" if any(k in tf for k in ("60", "1h", "hour", "hr", "day")) else "5_MIN"))
        target_sheet = f"{sym_clean}_{tf_str}"
    else:
        target_sheet = sym_clean

    snapshots, err = fetch_history_from_gsheet(symbol, sheet_name=target_sheet)
    if err:
        return jsonify({"history": [], "count": 0, "error": err})
    if not snapshots:
        return jsonify({"history": [], "count": 0})

    day_map = {"1day": 86400, "3day": 259200, "1week": 604800, "2week": 1209600, "1month": 2592000, "3month": 7776000}
    max_ts = max((s["timestamp"] for s in snapshots if s["timestamp"] > 0), default=0)
    if max_ts > 0 and day_range in day_map:
        cutoff = max_ts - day_map[day_range]
        snapshots = [s for s in snapshots if s["timestamp"] >= cutoff or s["timestamp"] == 0]

    if timeframe == "5minute" or not snapshots:
        return jsonify({"history": snapshots[-1500:], "count": len(snapshots[-1500:])})

    sec_map = {"15minute": 900, "30minute": 1800, "60minute": 3600, "day": 86400}
    interval_sec = sec_map.get(timeframe, 300)
    grouped = {}
    for idx, item in enumerate(snapshots):
        ts = item["timestamp"]
        bucket = (ts // interval_sec) * interval_sec if ts > 0 else (idx // (interval_sec // 300))
        grouped[bucket] = item
    resampled = [grouped[b] for b in sorted(grouped.keys())][-1500:]
    return jsonify({"history": resampled, "count": len(resampled)})

@app.route("/api/data")
def api_data():
    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry", "ALL").upper()
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)
    return jsonify({
        "rows": rows, "spot_price": spot_price, "atm_strike": atm_strike,
        "expiry": expiry, "expiries": expiries, "updated": updated,
        "pcr": pcr, "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi, "error": error
    })

@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return Response(status=200)
    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry", "ALL").upper()
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)
    return render_template_string(MAIN_HTML_TEMPLATE, rows=rows, symbol=symbol, spot_price=spot_price, atm_strike=atm_strike, expiry=expiry, expiries=expiries, updated=updated, pcr=pcr, total_ce_oi=total_ce_oi, total_pe_oi=total_pe_oi, error=error)

@app.route("/analysis", methods=["GET"])
def analysis_dashboard():
    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry", "ALL").upper()
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)
    return render_template_string(ANALYSIS_HTML_TEMPLATE, rows=rows, symbol=symbol, spot_price=spot_price, atm_strike=atm_strike, expiry=expiry, expiries=expiries, updated=updated, pcr=pcr, total_ce_oi=total_ce_oi, total_pe_oi=total_pe_oi, error=error)

@app.route("/download")
def download_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry", "ALL").upper()
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([f"Symbol: {symbol}", f"Spot Price: {spot_price}", f"ATM Strike: {atm_strike}", f"Expiry: {expiry}", f"PCR: {pcr}", f"Updated: {updated}"])
    cw.writerow(["CE OI", "CE Chg in OI", "CE Vol", "CE IV", "CE Delta", "CE Theta", "CE Vega", "CE Gamma", "CE Bid", "CE Ask", "CE LTP", "STRIKE", "PE LTP", "PE Bid", "PE Ask", "PE Delta", "PE Theta", "PE Vega", "PE Gamma", "PE IV", "PE Vol", "PE Chg in OI", "PE OI"])
    if not error:
        for r in rows:
            cw.writerow([r["ce_oi"], r["ce_oi_chg"], r["ce_vol"], r["ce_iv"], r["ce_delta"], r["ce_theta"], r["ce_vega"], r["ce_gamma"], r["ce_bid"], r["ce_ask"], r["ce_ltp"], r["strike"], r["pe_ltp"], r["pe_bid"], r["pe_ask"], r["pe_delta"], r["pe_theta"], r["pe_vega"], r["pe_gamma"], r["pe_iv"], r["pe_vol"], r["pe_oi_chg"], r["pe_oi"]])
    output = Response(si.getvalue(), mimetype="text/csv")
    output.headers["Content-Disposition"] = f"attachment; filename={symbol}_{expiry}_Option_Chain.csv"
    return output

@app.route("/download_history")
def download_history_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    snapshots, _ = fetch_history_from_gsheet(symbol)
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["Datetime", "Formatted Time", "Symbol", "Spot Price", "ATM Strike", "PCR", "CE OI", "PE OI", "CE Chg in OI", "PE Chg in OI", "CE Vol", "PE Vol", "CE IV", "PE IV", "CE Delta", "PE Delta", "CE Theta", "PE Theta", "CE Vega", "PE Vega", "CE Gamma", "PE Gamma", "CE Ask", "PE Bid", "Min ROC (Buy)", "Max ROC (Sell)", "Cross ROC Sig", "Main Volatility", "New Signal"])
    for s in snapshots:
        cw.writerow([s.get("datetime"), s.get("formatted_time"), s.get("symbol"), s.get("spot_price"), s.get("atm_strike"), s.get("pcr"), s.get("ce_oi"), s.get("pe_oi"), s.get("ce_oi_chg"), s.get("pe_oi_chg"), s.get("ce_vol"), s.get("pe_vol"), s.get("ce_iv"), s.get("pe_iv"), s.get("ce_delta"), s.get("pe_delta"), s.get("ce_theta"), s.get("pe_theta"), s.get("ce_vega"), s.get("pe_vega"), s.get("ce_gamma"), s.get("pe_gamma"), s.get("ce_ask"), s.get("pe_bid"), s.get("min_roc"), s.get("max_roc"), s.get("cross_roc"), s.get("main_volatility"), s.get("new_signal")])
    output = Response(si.getvalue(), mimetype="text/csv")
    output.headers["Content-Disposition"] = f"attachment; filename={symbol}_Historical_GSheet_Memory.csv"
    return output

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
