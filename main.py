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
import pyotp
from flask import Flask, render_template_string, request, Response, jsonify

# --- FLASK APP INITIALIZATION ---
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
AUTH_THREAD_LOCK = threading.Lock()
ORDER_EXECUTION_LOCK = threading.Lock()

INSTRUMENT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank"
}

OPEN_ORDER_STATUSES = {
    "open",
    "pending",
    "trigger pending",
    "open pending",
    "validation pending",
    "modify pending",
    "modify validation pending",
    "after market order req received",
    "put order req received",
    "scheduled",
    "active"
}

AUTH_COOLDOWN = {
    "last_failure_time": 0,
    "cooldown_duration": 1500  # 25 minutes safety lockout
}

def get_upstox_proxies():
    """Returns proxy dict if STATIC_IP_PROXY_URL or QUOTAGUARDSHIELD_URL is set."""
    proxy_url = (
        os.getenv("STATIC_IP_PROXY_URL")
        or os.getenv("QUOTAGUARDSHIELD_URL")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
    )
    if proxy_url and proxy_url.strip():
        clean_proxy = proxy_url.strip()
        return {"http": clean_proxy, "https": clean_proxy}
    return None

# --- PERSISTENT SESSION MANAGEMENT ---
def load_session_token():
    try:
        if os.path.exists(SESSION_FILE_PATH):
            with open(SESSION_FILE_PATH, "r") as f:
                data = json.load(f)
                if time.time() - data.get("generated_at", 0) < 43200:
                    return data.get("access_token")
    except Exception as e:
        print(f"[Session Warning] Failed to read session token: {e}")

    if ENV_ACCESS_TOKEN and ENV_ACCESS_TOKEN.strip():
        return ENV_ACCESS_TOKEN.strip()
    return None

def save_session_token(token):
    try:
        with open(SESSION_FILE_PATH, "w") as f:
            json.dump({
                "access_token": token,
                "generated_at": time.time()
            }, f)
    except Exception as e:
        print(f"[Session Warning] Failed to write session token: {e}")

def invalidate_session():
    if os.path.exists(SESSION_FILE_PATH):
        try:
            os.remove(SESSION_FILE_PATH)
        except Exception:
            pass

def perform_single_auth(force_refresh=False):
    with AUTH_THREAD_LOCK:
        if not force_refresh:
            existing_token = load_session_token()
            if existing_token:
                return existing_token

        current_time = time.time()
        if current_time - AUTH_COOLDOWN["last_failure_time"] < AUTH_COOLDOWN["cooldown_duration"]:
            remaining = int(AUTH_COOLDOWN["cooldown_duration"] - (current_time - AUTH_COOLDOWN["last_failure_time"]))
            print(f"[Auth Security Lock] Cooldown active! Login blocked for {remaining}s.")
            return None

        if not all([CLIENT_ID, USER_ID, PIN, TOTP_KEY]):
            if ENV_ACCESS_TOKEN and ENV_ACCESS_TOKEN.strip():
                return ENV_ACCESS_TOKEN.strip()
            print("[Auth Error] Missing required Upstox Environment Variables!")
            return None

        try:
            clean_totp_key = str(TOTP_KEY).strip().upper().replace(" ", "").replace("-", "")
            totp_code = str(pyotp.TOTP(clean_totp_key).now()).zfill(6)
            clean_pin = str(PIN).strip()
            clean_client_id = str(CLIENT_ID).strip()

            totp_url = "https://api.upstox.com/v2/totp-login/authorization/token"
            headers = {
                "accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": clean_client_id
            }
            payload = {
                "totp": totp_code,
                "pin": clean_pin
            }

            print("[Session Auth Attempt] Executing single TOTP login request...")
            res = requests.post(totp_url, json=payload, headers=headers, proxies=get_upstox_proxies(), timeout=10)

            if res.status_code == 200:
                json_data = res.json()
                token = json_data.get("access_token") or json_data.get("data", {}).get("access_token")
                if token:
                    save_session_token(token)
                    AUTH_COOLDOWN["last_failure_time"] = 0
                    print("[Session Auth Success] Token cached.")
                    return token
            
            AUTH_COOLDOWN["last_failure_time"] = time.time()
        except Exception as e:
            print(f"[Session Auth Exception] Error: {e}")
            AUTH_COOLDOWN["last_failure_time"] = time.time()

        return None

def get_valid_token():
    token = load_session_token()
    if token:
        return token
    return perform_single_auth()

# --- STATIC IP DETECTION & UPSTOX WHITELIST MANAGEMENT ---
def get_current_outbound_ip():
    """Fetches the exact outbound IPv4 address seen by external servers (respecting proxy if set)."""
    proxies = get_upstox_proxies()
    for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/ip"):
        try:
            res = requests.get(url, proxies=proxies, timeout=6)
            if res.status_code == 200:
                if "ipify" in url:
                    return res.json().get("ip", "").strip()
                return res.text.strip()
        except Exception:
            continue
    return ""

def fetch_upstox_static_ip_status():
    """Checks server outbound IP against Upstox registered Primary/Secondary Static IPs."""
    outbound_ip = get_current_outbound_ip()
    proxy_active = bool(get_upstox_proxies())

    token = get_valid_token()
    if not token:
        return {
            "outbound_ip": outbound_ip,
            "proxy_active": proxy_active,
            "primary_ip": None,
            "secondary_ip": None,
            "is_matched": False,
            "error": "Upstox access token unavailable."
        }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    try:
        res = requests.get("https://api.upstox.com/v2/user/ip", headers=headers, proxies=get_upstox_proxies(), timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", {}) or {}
            p_ip = data.get("primary_ip") or ""
            s_ip = data.get("secondary_ip") or ""
            matched = bool(outbound_ip and outbound_ip in (p_ip, s_ip))
            return {
                "outbound_ip": outbound_ip,
                "proxy_active": proxy_active,
                "primary_ip": p_ip,
                "secondary_ip": s_ip,
                "primary_ip_updated_at": data.get("primary_ip_updated_at"),
                "is_matched": matched,
                "error": None
            }
        else:
            resp_json = res.json() if res.content else {}
            err_msg = resp_json.get("errors", [{}])[0].get("message") if isinstance(resp_json.get("errors"), list) and resp_json.get("errors") else res.text
            return {
                "outbound_ip": outbound_ip,
                "proxy_active": proxy_active,
                "primary_ip": None,
                "secondary_ip": None,
                "is_matched": False,
                "error": f"HTTP {res.status_code}: {err_msg}"
            }
    except Exception as e:
        return {
            "outbound_ip": outbound_ip,
            "proxy_active": proxy_active,
            "primary_ip": None,
            "secondary_ip": None,
            "is_matched": False,
            "error": str(e)
        }

def update_upstox_static_ip(primary_ip, secondary_ip=None):
    """Registers Primary (and optional Secondary) Static IP via Upstox PUT /v2/user/ip and refreshes session."""
    token = get_valid_token()
    if not token:
        return False, "Upstox access token unavailable."

    clean_primary = str(primary_ip or "").strip()
    clean_secondary = str(secondary_ip or "").strip()

    if not clean_primary:
        return False, "Primary IP is required."

    payload = {"primary_ip": clean_primary}
    if clean_secondary and clean_secondary != clean_primary:
        payload["secondary_ip"] = clean_secondary

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    try:
        res = requests.put(
            "https://api.upstox.com/v2/user/ip",
            json=payload,
            headers=headers,
            proxies=get_upstox_proxies(),
            timeout=10
        )
        resp_json = res.json() if res.content else {}
        if res.status_code == 200 and resp_json.get("status") == "success":
            invalidate_session()
            new_token = perform_single_auth(force_refresh=True)
            reauth_note = "Session automatically refreshed via TOTP." if new_token else "Please update UPSTOX_ACCESS_TOKEN or re-authenticate."
            return True, f"Static IP Registered ({clean_primary})! {reauth_note}"
        else:
            err_msg = resp_json.get("errors", [{}])[0].get("message") if isinstance(resp_json.get("errors"), list) and resp_json.get("errors") else res.text
            return False, f"Upstox Rejected Static IP Update ({res.status_code}): {err_msg}"
    except Exception as e:
        return False, f"Static IP Update Exception: {e}"

# --- DASHBOARD SHEET ORDER CONFIGURATION & EXECUTION ENGINE ---
def get_ist_now():
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist_tz)

def get_ist_today_str():
    return get_ist_now().strftime("%Y-%m-%d")

def load_auto_order_state():
    today_str = get_ist_today_str()
    try:
        if os.path.exists(AUTO_ORDER_STATE_PATH):
            with open(AUTO_ORDER_STATE_PATH, "r") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return data
    except Exception as e:
        print(f"[Order State Warning] Read failed: {e}")
    return {"date": today_str, "recent_submissions": {}, "cancelled_signatures": []}

def save_auto_order_state(state):
    try:
        with open(AUTO_ORDER_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[Order State Warning] Write failed: {e}")

def round_tick(price, tick=0.05):
    if price is None or price <= 0:
        return 0.05
    return round(round(price / tick) * tick, 2)

def fetch_dashboard_order_config():
    """Fetches cells B16:B28 from Google Sheet tab 'DASHBOARD' with cache-busting and headers=0."""
    sheet_id = SPREADSHEET_ID or "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM"
    cache_buster = int(time.time() * 1000)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache"
    }
    urls = [
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&headers=0&sheet=DASHBOARD&range=A16:Z28&_cb={cache_buster}",
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&headers=0&sheet=DASHBOARD&_cb={cache_buster}"
    ]

    raw_csv = None
    last_err = None

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                text = res.text.strip().lstrip('\ufeff')
                if text.startswith("<!DOCTYPE") or "<html" in text.lower():
                    last_err = "Google Sheet is private. Set sharing to 'Anyone with the link can view'."
                    continue
                if len(text) > 5:
                    raw_csv = text
                    break
            else:
                last_err = f"HTTP {res.status_code} fetching DASHBOARD sheet."
        except Exception as e:
            last_err = f"Network error fetching DASHBOARD sheet: {e}"

    if not raw_csv:
        return None, last_err or "Could not fetch DASHBOARD sheet."

    try:
        lines = [line.lstrip('\ufeff') for line in raw_csv.splitlines() if line.strip()]
        grid = list(csv.reader(io.StringIO("\n".join(lines))))

        label_values = {}
        for r_idx, row in enumerate(grid):
            for c_idx, cell in enumerate(row):
                norm_key = re.sub(r'[^a-z0-9]+', '_', str(cell).strip().lower()).strip('_')
                if norm_key and c_idx + 1 < len(row):
                    val = str(row[c_idx + 1]).strip()
                    if norm_key not in label_values or val != "":
                        label_values[norm_key] = val

        def get_b_cell(row_num_1based, rel_idx_16):
            if len(grid) in (12, 13) and rel_idx_16 < len(grid) and len(grid[rel_idx_16]) >= 2:
                val = str(grid[rel_idx_16][1]).strip()
                if val != "":
                    return val
            if len(grid) >= row_num_1based and len(grid[row_num_1based - 1]) >= 2:
                val = str(grid[row_num_1based - 1][1]).strip()
                if val != "":
                    return val
            return ""

        def resolve_field(keys, row_num_1based, rel_idx_16):
            for k in keys:
                if k in label_values and label_values[k] != "":
                    return label_values[k]
            return get_b_cell(row_num_1based, rel_idx_16)

        manual_raw = resolve_field(["manual_order"], 16, 0)
        manual_mode = "Auto" if manual_raw.strip().lower() == "auto" else "Manual"

        want_raw = resolve_field(["want_to_trade"], 17, 1).strip().upper()
        want_to_trade = "YES" if want_raw in ("YES", "Y", "TRUE", "1") else "NO"

        amt_raw = resolve_field(["amount_to_be_invest", "amount_to_invest"], 18, 2).replace(",", "").strip()
        amount_to_invest = float(amt_raw) if amt_raw else 0.0

        sym_raw = resolve_field(["symbol"], 19, 3).strip().upper()
        symbol = "BANKNIFTY" if "BANK" in sym_raw else "NIFTY"

        expiry_month = resolve_field(["expiry_month"], 20, 4).strip().upper()

        strike_raw = resolve_field(["strike_price"], 21, 5).replace(",", "").strip()
        strike_price = int(float(strike_raw)) if strike_raw else 0

        opt_raw = resolve_field(["option_type"], 22, 6).strip().upper()
        option_type = opt_raw if opt_raw in ("CE", "PE") else "CE"

        txn_raw = resolve_field(["transaction_type"], 23, 7).strip().upper()
        transaction_type = txn_raw if txn_raw in ("BUY", "SELL") else "BUY"

        qty_raw = resolve_field(["quantity"], 24, 8).replace(",", "").strip()
        quantity = int(float(qty_raw)) if qty_raw else 0

        prod_raw = resolve_field(["product"], 25, 9).strip().upper()
        product = "I" if prod_raw in ("I", "INTRADAY", "MIS") else "D"

        sl_raw = resolve_field(["sl_percent"], 26, 10).replace("%", "").strip()
        sl_percent = float(sl_raw) if sl_raw != "" else None

        tgt_raw = resolve_field(["target_percent"], 27, 11).replace("%", "").strip()
        target_percent = float(tgt_raw) if tgt_raw != "" else None

        active_raw = resolve_field(["active_trade", "active_trades", "max_active_trade", "max_active_trades"], 28, 12).replace(",", "").strip()
        max_active_trades = int(float(active_raw)) if active_raw != "" else 1

        cfg = {
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
            "max_active_trades": max_active_trades
        }
        return cfg, None

    except Exception as e:
        return None, f"Error parsing DASHBOARD sheet B16:B28: {e}"

def resolve_monthly_expiry_date(symbol, expiry_month, headers):
    """Fetches option contracts and picks the last expiry date in the requested month."""
    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    expiry_url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}"
    res = requests.get(expiry_url, headers=headers, timeout=10)
    if res.status_code != 200:
        return None, f"Failed fetching option expiries: {res.text}"

    expiries = sorted(list(set(item["expiry"] for item in res.json().get("data", []))))
    if not expiries:
        return None, f"No option expiries found for {symbol}."

    month_lookup = {
        "JAN": 1, "JANUARY": 1, "01": 1, "1": 1,
        "FEB": 2, "FEBRUARY": 2, "02": 2, "2": 2,
        "MAR": 3, "MARCH": 3, "03": 3, "3": 3,
        "APR": 4, "APRIL": 4, "04": 4, "4": 4,
        "MAY": 5, "05": 5, "5": 5,
        "JUN": 6, "JUNE": 6, "06": 6, "6": 6,
        "JUL": 7, "JULY": 7, "07": 7, "7": 7,
        "AUG": 8, "AUGUST": 8, "08": 8, "8": 8,
        "SEP": 9, "SEPT": 9, "SEPTEMBER": 9, "09": 9, "9": 9,
        "OCT": 10, "OCTOBER": 10, "10": 10,
        "NOV": 11, "NOVEMBER": 11, "11": 11,
        "DEC": 12, "DECEMBER": 12, "12": 12
    }

    today_str = get_ist_today_str()
    future_expiries = [e for e in expiries if e >= today_str] or expiries

    target_m = month_lookup.get(str(expiry_month).strip().upper())
    if not target_m:
        if expiry_month in expiries:
            return expiry_month, None
        return future_expiries[0], None

    matching = [e for e in future_expiries if int(e.split("-")[1]) == target_m]
    if matching:
        first_year = sorted(list(set(e.split("-")[0] for e in matching)))[0]
        year_matches = [e for e in matching if e.startswith(first_year)]
        return max(year_matches), None

    return None, f"No expiry dates found in month '{expiry_month}' for {symbol}."

def prepare_evaluated_order(cfg):
    """
    Resolves monthly expiry, checks live LTP across strikes, enforces 'Amount to be Invest'
    by stepping to next OTM strike if needed, and computes SL & Target prices.
    """
    token = get_valid_token()
    if not token:
        return None, "Upstox token unavailable or authentication paused."

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    symbol = cfg["symbol"]
    expiry_date, exp_err = resolve_monthly_expiry_date(symbol, cfg["expiry_month"], headers)
    if exp_err or not expiry_date:
        return None, exp_err or "Could not resolve monthly expiry date."

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    _, chain_data = fetch_single_expiry_chain(instrument_key, expiry_date, headers)
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
        leg = item.get("call_options", {}) if opt_type == "CE" else item.get("put_options", {})
        leg = leg or {}
        mkt = leg.get("market_data", {}) or {}
        ltp = float(mkt.get("ltp", 0) or 0)
        inst_token = leg.get("instrument_key", "")
        if inst_token and ltp > 0:
            candidates.append({
                "strike": st,
                "ltp": ltp,
                "instrument_token": inst_token
            })

    if not candidates:
        return None, f"No active {opt_type} contracts with live LTP found for {symbol} ({expiry_date})."

    if opt_type == "CE":
        ordered = sorted([c for c in candidates if c["strike"] >= req_strike], key=lambda x: x["strike"])
    else:
        ordered = sorted([c for c in candidates if c["strike"] <= req_strike], key=lambda x: x["strike"], reverse=True)

    if not ordered:
        ordered = sorted(candidates, key=lambda x: abs(x["strike"] - req_strike))

    selected = None
    stepped_strikes = []
    for c in ordered:
        total_cost = round(c["ltp"] * qty, 2)
        if max_invest <= 0 or total_cost <= max_invest:
            selected = c
            selected["total_cost"] = total_cost
            break
        else:
            stepped_strikes.append(f"{int(c['strike'])} (₹{total_cost:,.0f})")

    if not selected:
        return None, f"Budget Exceeded: Even furthest {opt_type} strike exceeds Amount to be Invest (₹{max_invest:,.0f}). Checked: {', '.join(stepped_strikes[:4])}"

    entry_price = round_tick(selected["ltp"])
    total_investment = round(entry_price * qty, 2)
    txn_type = cfg["transaction_type"]

    sl_percent = cfg["sl_percent"]
    sl_price = None
    if sl_percent is not None and sl_percent > 0:
        if txn_type == "BUY":
            sl_price = round_tick(entry_price * (1.0 - sl_percent / 100.0))
        else:
            sl_price = round_tick(entry_price * (1.0 + sl_percent / 100.0))

    target_percent = cfg["target_percent"]
    target_price = None
    if target_percent is not None and target_percent > 0:
        if txn_type == "BUY":
            target_price = round_tick(entry_price * (1.0 + target_percent / 100.0))
        else:
            target_price = round_tick(entry_price * (1.0 - target_percent / 100.0))

    strike_adjusted = (int(selected["strike"]) != int(req_strike))
    signature = f"{cfg['manual_order']}|{symbol}|{expiry_date}|{int(selected['strike'])}|{opt_type}|{txn_type}|{qty}|{cfg['product']}|{sl_percent}|{target_percent}"

    evaluated = {
        "signature": signature,
        "manual_order": cfg["manual_order"],
        "want_to_trade": cfg["want_to_trade"],
        "symbol": symbol,
        "expiry_month": cfg["expiry_month"],
        "expiry_date": expiry_date,
        "requested_strike": int(req_strike),
        "selected_strike": int(selected["strike"]),
        "strike_adjusted": strike_adjusted,
        "stepped_reason": f"Adjusted from {int(req_strike)} to fit ₹{max_invest:,.0f} budget" if strike_adjusted else "Exact requested strike within budget",
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
    }
    return evaluated, None

def submit_upstox_evaluated_order(evaluated_order):
    """Places either a GTT Multi-Leg order (when SL or Target exists) or a standard Limit order via static IP/proxy."""
    token = get_valid_token()
    if not token:
        return False, "Authentication token missing or paused.", False

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
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
            rules = [
                {
                    "strategy": "ENTRY",
                    "trigger_type": "IMMEDIATE",
                    "trigger_price": entry_price
                }
            ]
            if target_price is not None:
                rules.append({
                    "strategy": "TARGET",
                    "trigger_type": "IMMEDIATE",
                    "trigger_price": target_price
                })
            if sl_price is not None:
                rules.append({
                    "strategy": "STOPLOSS",
                    "trigger_type": "IMMEDIATE",
                    "trigger_price": sl_price
                })

            payload = {
                "type": "MULTIPLE",
                "quantity": qty,
                "product": product,
                "rules": rules,
                "instrument_token": inst_token,
                "transaction_type": txn_type
            }
            url = "https://api.upstox.com/v3/order/gtt/place"
            res = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=12)
            resp_json = res.json() if res.content else {}

            if res.status_code == 200 and resp_json.get("status") == "success":
                gtt_ids = resp_json.get("data", {}).get("gtt_order_ids", [])
                return True, f"GTT Multi-Leg Order Placed! ID(s): {', '.join(gtt_ids) if gtt_ids else 'Confirmed'} | Entry: ₹{entry_price} | SL: {sl_price or 'Blank'} | Target: {target_price or 'Blank'}", False
            else:
                err_detail = resp_json.get("errors", [{}])[0].get("message") if isinstance(resp_json.get("errors"), list) and resp_json.get("errors") else res.text
                if res.status_code == 403 and "static IP" in err_detail:
                    cur_ip = get_current_outbound_ip()
                    return False, f"Upstox Static IP Block (403): Outbound IP '{cur_ip}' is not registered. Click 'Register Server IP' above or configure STATIC_IP_PROXY_URL.", True
                return False, f"Upstox GTT Order Rejected ({res.status_code}): {err_detail}", False

        else:
            payload = {
                "quantity": qty,
                "product": product,
                "validity": "DAY",
                "price": entry_price,
                "tag": "DASHBOARD_ORDER",
                "instrument_token": inst_token,
                "order_type": "LIMIT",
                "transaction_type": txn_type,
                "disclosed_quantity": 0,
                "trigger_price": 0,
                "is_amo": False
            }
            url = "https://api-hft.upstox.com/v3/order/place"
            res = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=12)
            resp_json = res.json() if res.content else {}

            if res.status_code == 200 and resp_json.get("status") == "success":
                order_ids = resp_json.get("data", {}).get("order_ids", [])
                order_id = order_ids[0] if order_ids else resp_json.get("data", {}).get("order_id", "Confirmed")
                return True, f"Limit Order Placed! Order ID: {order_id} | Entry: ₹{entry_price} | SL & Target: Blank (Self-Monitored)", False
            else:
                err_detail = resp_json.get("errors", [{}])[0].get("message") if isinstance(resp_json.get("errors"), list) and resp_json.get("errors") else res.text
                if res.status_code == 403 and "static IP" in err_detail:
                    cur_ip = get_current_outbound_ip()
                    return False, f"Upstox Static IP Block (403): Outbound IP '{cur_ip}' is not registered. Click 'Register Server IP' above or configure STATIC_IP_PROXY_URL.", True
                return False, f"Upstox Limit Order Rejected ({res.status_code}): {err_detail}", False

    except Exception as e:
        return False, f"Order API Exception: {e}", False

def extract_symbol_and_opt_type(trading_symbol):
    """Extracts underlying ('NIFTY' / 'BANKNIFTY') and option type ('CE' / 'PE') from Upstox trading_symbol."""
    ts = str(trading_symbol or "").strip().upper()
    if not ts:
        return None, None
    underlying = None
    if "BANKNIFTY" in ts:
        underlying = "BANKNIFTY"
    elif ts.startswith("NIFTY") or re.search(r'\bNIFTY\b', ts):
        underlying = "NIFTY"

    opt_match = re.search(r'(?:^|\s|\d)(CE|PE)(?:\s|$|\d)', ts)
    opt_type = opt_match.group(1) if opt_match else None
    return underlying, opt_type

def evaluate_trade_guards(target_symbol, target_opt_type, max_active_trades, positions, orders, gtt_orders=None):
    """
    Enforces:
    1) Active Trade Max Limit from cell B28 (blocks if active_count >= max_active_trades).
    2) Duplicate Script + Option Type Guard (blocks if same underlying + CE/PE is already active or in open order log across ANY strike, while allowing opposite CE/PE leg if B28 allows).
    """
    gtt_orders = gtt_orders or []
    active_items = []
    open_position_symbols = {}

    # 1. Active Open Positions (quantity != 0)
    for p in positions:
        qty = p.get("quantity", 0) or 0
        ts = str(p.get("trading_symbol", "")).strip().upper()
        if qty != 0 and ts:
            open_position_symbols[ts] = qty
            u, opt = extract_symbol_and_opt_type(ts)
            active_items.append({
                "source": "Active Position",
                "trading_symbol": ts,
                "underlying": u,
                "option_type": opt,
                "quantity": qty
            })

    # 2. Open / Pending Orders in Order Placement Log
    for o in orders:
        st = str(o.get("status", "")).strip().lower()
        ts = str(o.get("trading_symbol", "")).strip().upper()
        txn = str(o.get("transaction_type", "")).strip().upper()
        if st in OPEN_ORDER_STATUSES and ts:
            pos_qty = open_position_symbols.get(ts, 0)
            is_exit_leg = (pos_qty > 0 and txn == "SELL") or (pos_qty < 0 and txn == "BUY")
            if not is_exit_leg:
                u, opt = extract_symbol_and_opt_type(ts)
                active_items.append({
                    "source": f"Open Order ({o.get('status', '')})",
                    "trading_symbol": ts,
                    "underlying": u,
                    "option_type": opt,
                    "status": o.get("status", "")
                })

    # 3. Scheduled / Active GTT Entry Orders
    for g in gtt_orders:
        st = str(g.get("status", "")).strip().lower()
        ts = str(g.get("trading_symbol", "")).strip().upper()
        if st in OPEN_ORDER_STATUSES and ts and ts not in open_position_symbols:
            u, opt = extract_symbol_and_opt_type(ts)
            active_items.append({
                "source": f"Active GTT ({g.get('status', '')})",
                "trading_symbol": ts,
                "underlying": u,
                "option_type": opt,
                "status": g.get("status", "")
            })

    active_count = len(active_items)

    # Guard A: Duplicate Underlying + Option Type (CE/PE) check across any strike
    duplicate_matches = [
        item for item in active_items
        if item["underlying"] == str(target_symbol).upper() and item["option_type"] == str(target_opt_type).upper()
    ]
    if duplicate_matches:
        m = duplicate_matches[0]
        return {
            "allowed": False,
            "active_count": active_count,
            "max_active_trades": max_active_trades,
            "block_type": "DUPLICATE",
            "reason": f"Duplicate Blocked: {target_symbol} {target_opt_type} is already active/open ({m['trading_symbol']} - {m['source']})."
        }

    # Guard B: Active Trade Count Limit from Cell B28
    if max_active_trades is not None and active_count >= max_active_trades:
        return {
            "allowed": False,
            "active_count": active_count,
            "max_active_trades": max_active_trades,
            "block_type": "MAX_ACTIVE",
            "reason": f"Active Trade Limit Reached ({active_count}/{max_active_trades} active). Close existing trade/open order before placing new order."
        }

    return {
        "allowed": True,
        "active_count": active_count,
        "max_active_trades": max_active_trades,
        "block_type": None,
        "reason": f"Eligible ({active_count}/{max_active_trades} active trades)."
    }

# --- GOOGLE SHEET HISTORICAL & SIGNAL DATA LOADER ---
def parse_timestamp(dt_str):
    if not dt_str:
        return 0
    dt_str = str(dt_str).strip()
    date_formats = (
        "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", 
        "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M",
        "%m/%d/%y %H:%M:%S", "%d/%m/%y %H:%M:%S", "%m/%d/%y %H:%M", "%d/%m/%y %H:%M"
    )
    for fmt in date_formats:
        try:
            dt = datetime.strptime(dt_str, fmt)
            return int(dt.timestamp())
        except ValueError:
            pass
    return 0

def format_time_str(time_raw):
    time_str = str(time_raw).strip()
    if " " in time_str:
        parts = time_str.split(" ")
        time_part = parts[1] if len(parts) > 1 else parts[0]
    else:
        time_part = time_str
    t_parts = time_part.split(":")
    if len(t_parts) >= 2:
        return f"{t_parts[0]}:{t_parts[1]}"
    return time_str

def fetch_history_from_gsheet(symbol="NIFTY", sheet_name=None):
    sheet_id = SPREADSHEET_ID or "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM"
    
    if sheet_name:
        candidate_sheets = [sheet_name, sheet_name.upper(), symbol.upper(), "Sheet1", ""]
    else:
        candidate_sheets = ["NIFTY" if "BANK" not in symbol.upper() else "BANKNIFTY", symbol.upper(), "Sheet1", ""]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    raw_csv = None
    last_err = None

    for s_name in candidate_sheets:
        if s_name:
            url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(s_name)}"
        else:
            url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"

        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                text = res.text.strip().lstrip('\ufeff')
                if text.startswith("<!DOCTYPE") or "<html" in text.lower():
                    last_err = "Google Sheet is private. Set Sharing permissions to 'Anyone with the link can view'."
                    continue
                if len(text) > 30:
                    raw_csv = text
                    break
            else:
                last_err = f"Google Sheet HTTP status {res.status_code}"
        except Exception as e:
            last_err = f"Network error connecting to Google Sheet: {e}"

    if not raw_csv:
        err_msg = last_err or "Could not retrieve CSV from Google Sheet. Check Sheet ID and tab permissions."
        print(f"[GSheet Error] {err_msg}")
        return [], err_msg

    try:
        clean_csv = raw_csv.lstrip('\ufeff')
        lines = [line.strip().lstrip('\ufeff') for line in clean_csv.splitlines() if line.strip()]
        if not lines:
            return [], "Google Sheet CSV is empty."

        header_idx = 0
        for i, l in enumerate(lines[:10]):
            l_lower = l.lower()
            if "time" in l_lower or "symbol" in l_lower or "pcr" in l_lower or "ltp" in l_lower or "min roc" in l_lower:
                header_idx = i
                break

        csv_data_stream = "\n".join(lines[header_idx:])
        reader = csv.DictReader(io.StringIO(csv_data_stream))

        snapshots = []
        for row in reader:
            if not row:
                continue
            norm_r = {re.sub(r'^[^\w]+', '', str(k)).strip().lower(): str(v).strip() for k, v in row.items() if k is not None}

            def safe_num(primary_name, alt_names=[], default=0.0):
                names = [primary_name.lower()] + [a.lower() for a in alt_names]
                for n in names:
                    if n in norm_r:
                        val_str = norm_r[n].replace(",", "").strip()
                        try:
                            return float(val_str)
                        except (ValueError, TypeError):
                            pass
                    n_nospace = n.replace(" ", "")
                    for k_key, k_val in norm_r.items():
                        if k_key.replace(" ", "") == n_nospace:
                            try:
                                return float(k_val.replace(",", "").strip())
                            except (ValueError, TypeError):
                                pass
                return default

            def safe_str(primary_name, alt_names=[]):
                names = [primary_name.lower()] + [a.lower() for a in alt_names]
                for n in names:
                    if n in norm_r:
                        return norm_r[n]
                    n_nospace = n.replace(" ", "")
                    for k_key, k_val in norm_r.items():
                        if k_key.replace(" ", "") == n_nospace:
                            return k_val
                return ""

            time_val = str(norm_r.get("time", norm_r.get("datetime", norm_r.get("timestamp", norm_r.get("date & time", norm_r.get("date and time", ""))))))
            if not time_val or time_val.lower() in ("nan", "none", "", "0.0"):
                continue

            snapshot = {
                "timestamp": parse_timestamp(time_val),
                "datetime": time_val,
                "formatted_time": format_time_str(time_val),
                "symbol": str(norm_r.get("symbol", symbol.upper())),
                "spot_price": safe_num("Price LTP", ["spot_price", "ltp"]),
                "atm_strike": safe_num("ATM Strike", ["atm_strike"]),
                "pcr": safe_num("PCR"),
                "ce_oi": safe_num("CE OI"),
                "pe_oi": safe_num("PE OI"),
                "ce_oi_chg": safe_num("CE Chg in OI", ["CE Chng OI", "CE Chg OI"]),
                "pe_oi_chg": safe_num("PE Chg in OI", ["PE Chng OI", "PE Chg OI"]),
                "ce_vol": safe_num("CE Vol"),
                "pe_vol": safe_num("PE Vol"),
                "ce_iv": safe_num("CE IV"),
                "pe_iv": safe_num("PE IV"),
                "ce_delta": safe_num("CE Delta"),
                "pe_delta": safe_num("PE Delta"),
                "ce_theta": safe_num("CE Theta"),
                "pe_theta": safe_num("PE Theta"),
                "ce_vega": safe_num("CE Vega"),
                "pe_vega": safe_num("PE Vega"),
                "ce_gamma": safe_num("CE Gamma"),
                "pe_gamma": safe_num("PE Gamma"),
                "ce_bid": safe_num("CE Bid"),
                "pe_bid": safe_num("PE Bid"),
                "ce_ask": safe_num("CE Ask"),
                "pe_ask": safe_num("PE Ask"),
                "ce_ltp": safe_num("CE LTP"),
                "pe_ltp": safe_num("PE LTP"),
                "min_roc": safe_num("Min ROC (Buy)", ["min_roc", "min roc"]),
                "max_roc": safe_num("Max ROC (Sell)", ["max_roc", "max roc"]),
                "cross_roc": safe_num("Cross ROC Sig", ["cross_roc_sig", "cross roc"]),
                "main_volatility": safe_num("Main Volatility", ["main_vol", "volatility"]),
                "new_signal": safe_str("New Signal", ["signal", "sig"])
            }
            snapshots.append(snapshot)

        if not snapshots:
            return [], f"Google Sheet connected ({sheet_name or symbol}) but no valid data rows were found."

        return snapshots[::-1], None

    except Exception as e:
        print(f"[GSheet Parsing Exception]: {e}")
        return [], f"Parsing error reading Google Sheet CSV: {e}"

# --- UPSTOX LIVE TRADE & POSITION FETCHING ---
def fetch_upstox_positions():
    token = get_valid_token()
    if not token:
        return [], [], [], 0.0, 0.0, 0.0, "Authentication paused or invalid."

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    try:
        pos_url = "https://api.upstox.com/v2/portfolio/short-term-positions"
        pos_res = requests.get(pos_url, headers=headers, timeout=10)

        if pos_res.status_code == 401:
            invalidate_session()
            token = perform_single_auth(force_refresh=True)
            if not token:
                return [], [], [], 0.0, 0.0, 0.0, "Session expired."
            headers["Authorization"] = f"Bearer {token}"
            pos_res = requests.get(pos_url, headers=headers, timeout=10)

        positions_raw = pos_res.json().get("data", []) if pos_res.status_code == 200 else []

        ord_url = "https://api.upstox.com/v2/order/retrieve-all"
        ord_res = requests.get(ord_url, headers=headers, timeout=10)
        orders_raw = ord_res.json().get("data", []) if ord_res.status_code == 200 else []

        gtt_orders = []
        try:
            gtt_res = requests.get("https://api.upstox.com/v3/order/gtt", headers=headers, timeout=8)
            if gtt_res.status_code == 200:
                gtt_raw = gtt_res.json().get("data", []) or []
                for g in gtt_raw:
                    rules = g.get("rules", []) or []
                    entry_rule = next((r for r in rules if str(r.get("strategy", "")).upper() == "ENTRY"), rules[0] if rules else {})
                    gtt_orders.append({
                        "gtt_order_id": g.get("gtt_order_id", ""),
                        "trading_symbol": g.get("trading_symbol", ""),
                        "transaction_type": g.get("transaction_type", ""),
                        "quantity": g.get("quantity", 0),
                        "status": entry_rule.get("status") or g.get("status", "")
                    })
        except Exception:
            pass

        positions = []
        total_pnl = 0.0
        realised_pnl = 0.0
        unrealised_pnl = 0.0

        for p in positions_raw:
            trading_symbol = p.get("trading_symbol", "")
            quantity = p.get("quantity", 0)
            buy_price = p.get("buy_price", 0.0) or 0.0
            sell_price = p.get("sell_price", 0.0) or 0.0
            ltp = p.get("last_price", 0.0) or 0.0
            pnl = p.get("pnl", 0.0) or 0.0
            u_pnl = p.get("unrealised", 0.0) or 0.0
            r_pnl = p.get("realised", 0.0) or 0.0
            product = p.get("product", "")

            total_pnl += pnl
            unrealised_pnl += u_pnl
            realised_pnl += r_pnl

            positions.append({
                "trading_symbol": trading_symbol,
                "product": product,
                "quantity": quantity,
                "buy_price": round(buy_price, 2),
                "sell_price": round(sell_price, 2),
                "ltp": round(ltp, 2),
                "pnl": round(pnl, 2),
                "status": "OPEN" if quantity != 0 else "CLOSED"
            })

        orders = []
        for o in orders_raw:
            orders.append({
                "order_id": o.get("order_id", ""),
                "trading_symbol": o.get("trading_symbol", ""),
                "transaction_type": o.get("transaction_type", ""),
                "quantity": o.get("quantity", 0),
                "price": o.get("price", 0.0),
                "average_price": o.get("average_price", 0.0),
                "status": o.get("status", ""),
                "order_timestamp": o.get("order_timestamp", "")
            })

        return positions, orders, gtt_orders, round(total_pnl, 2), round(realised_pnl, 2), round(unrealised_pnl, 2), None

    except Exception as e:
        print(f"[Positions Exception] Error fetching positions: {e}")
        return [], [], [], 0.0, 0.0, 0.0, str(e)

# --- UPSTOX CONCURRENT DATA FETCHING ---
def fetch_single_expiry_chain(instrument_key, expiry_date, headers):
    try:
        chain_url = f"https://api.upstox.com/v2/option/chain?instrument_key={urllib.parse.quote(instrument_key)}&expiry_date={expiry_date}"
        res = requests.get(chain_url, headers=headers, timeout=10)
        if res.status_code == 200:
            return expiry_date, res.json().get("data", [])
    except Exception as e:
        print(f"[Fetch Error] Failed fetching expiry {expiry_date}: {e}")
    return expiry_date, []

def fetch_option_chain(symbol, selected_expiry="ALL"):
    token = get_valid_token()
    if not token:
        return None, None, [], "Authentication paused to protect account from lockout. Wait 25 mins or check logs."

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    try:
        expiry_url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}"
        exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code == 401:
            invalidate_session()
            token = perform_single_auth(force_refresh=True)
            if not token:
                return None, None, [], "Session expired and re-authentication paused."
            headers["Authorization"] = f"Bearer {token}"
            exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code != 200:
            return None, None, [], f"Expiry lookup failed: {exp_res.text}"

        expiries = sorted(list(set(item["expiry"] for item in exp_res.json().get("data", []))))
        if not expiries:
            return None, None, [], "No option contract expiries found."

        clean_expiry = str(selected_expiry).strip().upper() if selected_expiry else "ALL"

        if clean_expiry == "ALL" or clean_expiry == "ALL EXPIRIES (CONSOLIDATED)" or clean_expiry not in expiries:
            target_expiries = expiries[:6]
            results_dict = {}

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(fetch_single_expiry_chain, instrument_key, exp, headers): exp for exp in target_expiries}
                for future in as_completed(futures):
                    exp_date, data = future.result()
                    if data:
                        results_dict[exp_date] = data

            combined_chain = []
            for exp in sorted(results_dict.keys()):
                combined_chain.extend(results_dict[exp])
            
            return combined_chain, "ALL EXPIRIES (CONSOLIDATED)", expiries, None

        _, chain_data = fetch_single_expiry_chain(instrument_key, clean_expiry, headers)
        return chain_data, clean_expiry, expiries, None

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

    num_strikes_each_side = 12
    min_strike = atm_strike - (num_strikes_each_side * step)
    max_strike = atm_strike + (num_strikes_each_side * step)

    strike_map = {}

    for item in chain_data:
        strike = item.get("strike_price", 0)
        if not (min_strike <= strike <= max_strike):
            continue

        if strike not in strike_map:
            strike_map[strike] = {
                "strike": strike,
                "is_atm": (strike == atm_strike),
                "ce_oi": 0, "ce_oi_chg": 0, "ce_vol": 0, "ce_bid_qty": 0, "ce_ask_qty": 0,
                "ce_iv": 0, "ce_ltp": 0, "ce_bid": 0, "ce_ask": 0, "ce_delta": 0, "ce_theta": 0, "ce_vega": 0, "ce_gamma": 0,
                "pe_oi": 0, "pe_oi_chg": 0, "pe_vol": 0, "pe_bid_qty": 0, "pe_ask_qty": 0,
                "pe_iv": 0, "pe_ltp": 0, "pe_bid": 0, "pe_ask": 0, "pe_delta": 0, "pe_theta": 0, "pe_vega": 0, "pe_gamma": 0,
                "_has_pricing": False
            }

        rec = strike_map[strike]

        ce = item.get("call_options", {}) or {}
        pe = item.get("put_options", {}) or {}

        ce_mkt = ce.get("market_data", {}) or {}
        ce_gk = ce.get("option_greeks", {}) or {}
        pe_mkt = pe.get("market_data", {}) or {}
        pe_gk = pe.get("option_greeks", {}) or {}

        c_oi = ce_mkt.get("oi", 0) or 0
        p_oi = pe_mkt.get("oi", 0) or 0

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

    total_ce_oi = sum(r["ce_oi"] for r in rows)
    total_pe_oi = sum(r["pe_oi"] for r in rows)
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0

    return rows, spot_price, atm_strike, current_expiry, expiries, updated_time, pcr, total_ce_oi, total_pe_oi, None

# --- MAIN TABLE TEMPLATE ---
MAIN_HTML_TEMPLATE = """
<!DOCTYPE html>
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
        .btn:hover { opacity: 0.9; }
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
        .greek-col { font-size: 12px; color: #495057; }
        .order-col { font-size: 12px; color: #495057; }
        
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
                    {% for exp in expiries %}
                        <option value="{{ exp }}" {% if exp == expiry %}selected{% endif %}>{{ exp }}</option>
                    {% endfor %}
                </select>
            </form>

            <a href="/analysis?symbol={{ symbol }}&expiry={{ expiry }}" class="btn btn-purple">📊 Open Analysis Dashboard</a>
            <a href="/download?symbol={{ symbol }}&expiry={{ expiry }}" class="btn btn-success">Download Full CSV</a>
        </div>
    </div>

    {% if error %}
        <div class="error-msg">Error: {{ error }}</div>
    {% else %}
    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th colspan="11" class="ce-header">CALL OPTIONS (CE)</th>
                    <th>STRIKE</th>
                    <th colspan="11" class="pe-header">PUT OPTIONS (PE)</th>
                </tr>
                <tr>
                    <th>OI</th><th>Chg in OI</th><th>Vol</th><th>IV</th><th>Delta</th><th>Theta</th><th>Vega</th><th>Gamma</th><th>Bid</th><th>Ask</th><th>LTP</th>
                    <th>Price</th>
                    <th>LTP</th><th>Bid</th><th>Ask</th><th>Delta</th><th>Theta</th><th>Vega</th><th>Gamma</th><th>IV</th><th>Vol</th><th>Chg in OI</th><th>OI</th>
                </tr>
            </thead>
            <tbody>
                {% for row in rows %}
                <tr>
                    <td class="ce-cell">{{ "{:,}".format(row.ce_oi) }}</td>
                    <td class="ce-cell">{{ "{:,}".format(row.ce_oi_chg) }}</td>
                    <td class="ce-cell">{{ "{:,}".format(row.ce_vol) }}</td>
                    <td class="ce-cell">{{ row.ce_iv }}</td>
                    <td class="ce-cell greek-col">{{ row.ce_delta }}</td>
                    <td class="ce-cell greek-col">{{ row.ce_theta }}</td>
                    <td class="ce-cell greek-col">{{ row.ce_vega }}</td>
                    <td class="ce-cell greek-col">{{ row.ce_gamma }}</td>
                    <td class="ce-cell order-col">{{ row.ce_bid }}</td>
                    <td class="ce-cell order-col">{{ row.ce_ask }}</td>
                    <td class="ce-cell"><strong>{{ row.ce_ltp }}</strong></td>
                    
                    <td class="{% if row.is_atm %}atm-spot{% else %}strike-normal{% endif %}">
                        {% if row.is_atm %}★ {{ row.strike }} (SPOT){% else %}{{ row.strike }}{% endif %}
                    </td>
                    
                    <td class="pe-cell"><strong>{{ row.pe_ltp }}</strong></td>
                    <td class="pe-cell order-col">{{ row.pe_bid }}</td>
                    <td class="pe-cell order-col">{{ row.pe_ask }}</td>
                    <td class="pe-cell greek-col">{{ row.pe_delta }}</td>
                    <td class="pe-cell greek-col">{{ row.pe_theta }}</td>
                    <td class="pe-cell greek-col">{{ row.pe_vega }}</td>
                    <td class="pe-cell greek-col">{{ row.pe_gamma }}</td>
                    <td class="pe-cell">{{ row.pe_iv }}</td>
                    <td class="pe-cell">{{ "{:,}".format(row.pe_vol) }}</td>
                    <td class="pe-cell">{{ "{:,}".format(row.pe_oi_chg) }}</td>
                    <td class="pe-cell">{{ "{:,}".format(row.pe_oi) }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% endif %}
</body>
</html>
"""

# --- ANALYSIS DASHBOARD TEMPLATE ---
ANALYSIS_HTML_TEMPLATE = """
<!DOCTYPE html>
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
        .btn-dl:hover { background-color: #059669; }
        .btn:hover { opacity: 0.9; }
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
            {% for exp in expiries %}
                <option value="{{ exp }}" {% if exp == expiry %}selected{% endif %}>{{ exp }}</option>
            {% endfor %}
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
        <div class="card" style="border-left-color: #facc15;">
            <div class="card-title">Spot Price / ATM</div>
            <div class="card-value" id="cardSpot" style="color: #facc15;">{{ spot_price }} (ATM: {{ atm_strike }})</div>
        </div>
        <div class="card" id="cardPcrBox">
            <div class="card-title">Put-Call Ratio (PCR)</div>
            <div class="card-value" id="cardPcr">{{ pcr }}</div>
        </div>
        <div class="card" style="border-left-color: #ef4444;">
            <div class="card-title">Major Resistance (Max Call OI)</div>
            <div class="card-value" id="maxCallStrike" style="color: #ef4444;">-</div>
        </div>
        <div class="card" style="border-left-color: #22c55e;">
            <div class="card-title">Major Support (Max Put OI)</div>
            <div class="card-value" id="maxPutStrike" style="color: #22c55e;">-</div>
        </div>
        <div class="card" style="border-left-color: #a855f7;">
            <div class="card-title">Last Updated (IST)</div>
            <div class="card-value" id="cardUpdated" style="font-size: 15px;">{{ updated }}</div>
        </div>
    </div>

    <div class="sentiment-bar-container">
        <div style="display: flex; justify-content: space-between; font-weight: bold; margin-bottom: 8px;">
            <span style="color: #22c55e;" id="gaugePeLabel">PUT (PE) TOTAL: -</span>
            <span style="color: #38bdf8;" id="gaugeMetricName">SELECTED METRIC COMPARISON</span>
            <span style="color: #ef4444;" id="gaugeCeLabel">CALL (CE) TOTAL: -</span>
        </div>
        <div class="gauge-flex">
            <div id="gaugePeBar" class="gauge-pe" style="width: 50%;"></div>
            <div id="gaugeCeBar" class="gauge-ce" style="width: 50%;"></div>
        </div>
        <div style="display: flex; justify-content: space-between; color: #94a3b8; font-size: 12px; margin-top: 6px;">
            <span id="gaugePeAvg">Put Avg: -</span>
            <span id="gaugeRatioText">Ratio: 1.00</span>
            <span id="gaugeCeAvg">Call Avg: -</span>
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
            <div style="display: flex; align-items: center; gap: 15px;">
                <span class="chart-title">2 - 📈 Historical Snapshot Trend (Google Sheet Memory)</span>
            </div>
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

                <label style="color: #94a3b8; font-weight: bold; margin-left: 6px;">Data Day Range:</label>
                <select id="historyDayRangeSelector" onchange="loadHistoricalChart(true)">
                    <option value="1day" selected>1 Day</option>
                    <option value="3day">3 Day</option>
                    <option value="1week">1 Week</option>
                    <option value="2week">2 Week</option>
                    <option value="1month">1 Month</option>
                    <option value="3month">3 Month</option>
                </select>

                <label style="color: #94a3b8; font-weight: bold; margin-left: 6px;">Interval:</label>
                <select id="historyTimeframeSelector" onchange="loadHistoricalChart(true)">
                    <option value="5minute" selected>5 Min</option>
                    <option value="15minute">15 Min</option>
                    <option value="30minute">30 Min</option>
                    <option value="60minute">1 Hour</option>
                    <option value="day">1 Day</option>
                </select>

                <a id="downloadHistoryBtn" href="/download_history?symbol={{ symbol }}" class="btn-dl">📥 Download Google Sheet Memory CSV</a>
            </div>
        </div>
        <canvas id="historyChart" style="max-height: 420px;"></canvas>
    </div>

    <div class="chart-box">
        <div class="chart-header">
            <div style="display: flex; align-items: center; gap: 15px;">
                <span class="chart-title">3 - 🚀 Main Signal</span>
            </div>
            <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
                <label style="color: #94a3b8; font-weight: bold;">Data Day Range:</label>
                <select id="signalDayRangeSelector" onchange="loadSignalChart(true)">
                    <option value="1day" selected>1 Day</option>
                    <option value="3day">3 Day</option>
                    <option value="1week">1 Week</option>
                    <option value="2week">2 Week</option>
                    <option value="1month">1 Month</option>
                    <option value="3month">3 Month</option>
                </select>

                <label style="color: #94a3b8; font-weight: bold; margin-left: 6px;">Interval:</label>
                <select id="signalTimeframeSelector" onchange="loadSignalChart(true)">
                    <option value="5minute" selected>5 Min</option>
                    <option value="15minute">15 Min</option>
                    <option value="30minute">30 Min</option>
                    <option value="60minute">1 Hour</option>
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
        let myChart = null;
        let historyChartObj = null;
        let signalChartObj = null;
        let hasHistoryData = false;
        let hasSignalData = false;

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
                sumX += i;
                sumY += val;
                sumXY += i * val;
                sumXX += i * i;
            }
            const denom = (n * sumXX - sumX * sumX);
            const slope = denom !== 0 ? (n * sumXY - sumX * sumY) / denom : 0;
            const intercept = (sumY - slope * sumX) / n;

            return data.map((_, i) => Number((slope * i + intercept).toFixed(2)));
        }

        const signalLabelPlugin = {
            id: 'signalLabelPlugin',
            afterDatasetsDraw: (chart) => {
                const { ctx } = chart;
                chart.data.datasets.forEach((dataset, datasetIndex) => {
                    if (dataset.label === 'Main Volatility') {
                        const meta = chart.getDatasetMeta(datasetIndex);
                        const signals = dataset.signalLabels || [];
                        meta.data.forEach((element, index) => {
                            const sig = signals[index];
                            if (sig && (sig.toUpperCase() === 'B' || sig.toUpperCase() === 'S')) {
                                const { x, y } = element.tooltipPosition();
                                ctx.save();
                                ctx.fillStyle = '#ffffff';
                                ctx.font = 'bold 13px Segoe UI';
                                ctx.textAlign = 'center';
                                ctx.textBaseline = 'bottom';
                                ctx.fillText(sig.toUpperCase(), x, y - 6);
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
                const y = scales.y;
                const zeroY = y.getPixelForValue(0);

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
                    ctx.setLineDash([]);
                    ctx.stroke();
                } else if (zeroY > bottom) {
                    ctx.fillStyle = 'rgba(34, 197, 94, 0.08)';
                    ctx.fillRect(left, top, right - left, bottom - top);
                } else if (zeroY < top) {
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.08)';
                    ctx.fillRect(left, top, right - left, bottom - top);
                }
                ctx.restore();
            }
        };

        function initSignalChart() {
            const ctx = document.getElementById('signalChart').getContext('2d');
            signalChartObj = new Chart(ctx, {
                type: 'line',
                data: { labels: [], datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    animations: { colors: false, x: false, y: false },
                    transitions: { active: { animation: { duration: 0 } } },
                    scales: {
                        x: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
                        y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } }
                    },
                    plugins: {
                        legend: { labels: { color: '#f8fafc', font: { size: 13 } } },
                        tooltip: { mode: 'index', intersect: false }
                    }
                },
                plugins: [zeroLinePlugin, signalLabelPlugin]
            });
            loadSignalChart(true);
        }

        function loadSignalChart(isManualChange = false) {
            const dayRange = document.getElementById('signalDayRangeSelector').value;
            const timeframe = document.getElementById('signalTimeframeSelector').value;

            fetch(`/api/history?symbol=${currentSymbol}&timeframe=${timeframe}&day_range=${dayRange}&chart_type=signal`)
                .then(res => res.json())
                .then(data => {
                    if (data.error) {
                        if (isManualChange || !hasSignalData) {
                            signalChartObj.data.labels = [data.error];
                            signalChartObj.data.datasets = [{ label: 'Status / Error', data: [0], borderColor: '#ef4444' }];
                            signalChartObj.update('none');
                        }
                        return;
                    }

                    const snapshots = data.history || [];
                    if (snapshots.length === 0) {
                        if (isManualChange || !hasSignalData) {
                            signalChartObj.data.labels = ["No data found for Main Signal."];
                            signalChartObj.data.datasets = [{ label: 'Status', data: [0], borderColor: '#94a3b8' }];
                            signalChartObj.update('none');
                        }
                        return;
                    }

                    hasSignalData = true;
                    const times = snapshots.map(s => s.formatted_time || s.datetime);
                    const minRocVals = snapshots.map(s => s.min_roc || 0);
                    const maxRocVals = snapshots.map(s => s.max_roc || 0);
                    const crossRocVals = snapshots.map(s => s.cross_roc || 0);
                    const mainVolVals = snapshots.map(s => s.main_volatility || 0);
                    const newSignals = snapshots.map(s => s.new_signal || "");

                    signalChartObj.data.labels = times;
                    signalChartObj.data.datasets = [
                        { label: 'Min ROC (Buy)', data: minRocVals, borderColor: '#22c55e', backgroundColor: 'rgba(34, 197, 94, 0.1)', borderWidth: 2, tension: 0.2 },
                        { label: 'Max ROC (Sell)', data: maxRocVals, borderColor: '#ef4444', backgroundColor: 'rgba(239, 68, 68, 0.1)', borderWidth: 2, tension: 0.2 },
                        { label: 'Cross ROC Sig', data: crossRocVals, borderColor: '#facc15', backgroundColor: 'rgba(250, 204, 21, 0.1)', borderWidth: 2, tension: 0.2 },
                        { label: 'Main Volatility', data: mainVolVals, signalLabels: newSignals, borderColor: '#38bdf8', backgroundColor: 'rgba(56, 189, 248, 0.1)', borderWidth: 3, pointRadius: 4, pointHoverRadius: 6, tension: 0.2 }
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
                        x: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
                        y: { position: 'left', ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
                        y1: { position: 'right', display: false, ticks: { color: '#38bdf8' }, grid: { drawOnChartArea: false } }
                    },
                    plugins: {
                        legend: { labels: { color: '#f8fafc', font: { size: 13 } } },
                        tooltip: { mode: 'index', intersect: false }
                    }
                }
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
                    if (data.error) {
                        if (isManualChange || !hasHistoryData) {
                            historyChartObj.data.labels = [data.error];
                            historyChartObj.data.datasets = [{ label: 'Status / Error', data: [0], borderColor: '#ef4444' }];
                            historyChartObj.update('none');
                        }
                        return;
                    }

                    const snapshots = data.history || [];
                    if (snapshots.length === 0) {
                        if (isManualChange || !hasHistoryData) {
                            historyChartObj.data.labels = ["No data found in Google Sheet."];
                            historyChartObj.data.datasets = [{ label: 'Status', data: [0], borderColor: '#94a3b8' }];
                            historyChartObj.update('none');
                        }
                        return;
                    }

                    hasHistoryData = true;
                    const times = snapshots.map(s => s.formatted_time || s.datetime);
                    let datasets = [];

                    if (metric === "price_pcr") {
                        historyChartObj.options.scales.y1.display = true;
                        const spotVals = snapshots.map(s => s.spot_price);
                        const pcrVals = snapshots.map(s => s.pcr);

                        datasets.push({ label: 'Relation 2: PCR', data: pcrVals, borderColor: '#06b6d4', backgroundColor: 'rgba(6, 182, 212, 0.1)', borderWidth: 2, tension: 0.2, yAxisID: 'y', fill: false });
                        datasets.push({ label: 'Relation 1: Price LTP', data: spotVals, borderColor: '#facc15', backgroundColor: 'rgba(250, 204, 21, 0.1)', borderWidth: 2, tension: 0.2, yAxisID: 'y1', fill: true });
                        datasets.push({ label: 'Price Trend', data: computeLinearTrend(spotVals), borderColor: '#ca8a04', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, yAxisID: 'y1', fill: false });
                    }
                    else {
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

                        datasets.push({ label: `Relation 1: ${cfg.r1}`, data: ceVals, borderColor: '#ef4444', backgroundColor: 'rgba(239, 68, 68, 0.1)', borderWidth: 2, tension: 0.2, yAxisID: 'y' });
                        datasets.push({ label: `Relation 2: ${cfg.r2}`, data: peVals, borderColor: '#22c55e', backgroundColor: 'rgba(34, 197, 94, 0.1)', borderWidth: 2, tension: 0.2, yAxisID: 'y' });
                        datasets.push({ label: `Average (${cfg.r1} & ${cfg.r2})`, data: avgVals, borderColor: '#facc15', backgroundColor: 'rgba(250, 204, 21, 0.1)', borderWidth: 2, tension: 0.2, yAxisID: 'y', fill: false });
                        datasets.push({ label: `${cfg.r1} Trend`, data: computeLinearTrend(ceVals), borderColor: '#f87171', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, yAxisID: 'y', fill: false });
                        datasets.push({ label: `${cfg.r2} Trend`, data: computeLinearTrend(peVals), borderColor: '#4ade80', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, yAxisID: 'y', fill: false });
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
                        const topY = chart.scales.y.top;
                        const bottomY = chart.scales.y.bottom;
                        const ctx = chart.ctx;
                        ctx.save();
                        ctx.beginPath();
                        ctx.moveTo(x, topY);
                        ctx.lineTo(x, bottomY);
                        ctx.lineWidth = 3;
                        ctx.strokeStyle = '#facc15';
                        ctx.setLineDash([6, 4]);
                        ctx.stroke();

                        ctx.fillStyle = '#facc15';
                        ctx.font = 'bold 12px Segoe UI';
                        ctx.fillText('★ ATM (' + currentAtmStrike + ')', x + 5, topY + 15);
                        ctx.restore();
                    }
                }
            }
        };

        function renderPageData(data) {
            if (data.error) {
                document.getElementById("errorContainer").style.display = "block";
                document.getElementById("errorContainer").innerText = "Error: " + data.error;
                return;
            }
            document.getElementById("errorContainer").style.display = "none";

            rawRows = data.rows || [];
            currentAtmStrike = data.atm_strike || 0;
            
            document.getElementById("cardSpot").innerText = `${data.spot_price || 0} (ATM: ${currentAtmStrike})`;
            document.getElementById("cardUpdated").innerText = data.updated || "";
            
            const pcrVal = data.pcr || 0;
            const pcrBox = document.getElementById("cardPcrBox");
            if (pcrVal >= 1.0) {
                pcrBox.style.borderLeftColor = "#22c55e";
                document.getElementById("cardPcr").innerHTML = `${pcrVal} <span style="color:#22c55e; font-size:14px;">(Bullish)</span>`;
            } else {
                pcrBox.style.borderLeftColor = "#ef4444";
                document.getElementById("cardPcr").innerHTML = `${pcrVal} <span style="color:#ef4444; font-size:14px;">(Bearish)</span>`;
            }

            let maxCE = -1, resStrike = 0;
            let maxPE = -1, supStrike = 0;
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
                        { type: 'line', label: 'PE Trend', data: [], borderColor: '#4ade80', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, fill: false },
                        { type: 'line', label: 'CE Trend', data: [], borderColor: '#f87171', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, fill: false }
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
                    plugins: {
                        legend: { labels: { color: '#f8fafc', font: { size: 13 } } },
                        tooltip: { mode: 'index', intersect: false }
                    }
                },
                plugins: [spotLinePlugin]
            });
        }

        function updateChartMetric() {
            const selected = document.getElementById("metricSelector").value;
            const titleEl = document.getElementById("chartHeaderTitle");
            const strikes = rawRows.map(r => r.strike);

            let peData = [], ceData = [];
            let peLabel = "", ceLabel = "";
            let peColor = "", ceColor = "";
            let metricTitle = "";

            if (selected === "depth_qty") {
                metricTitle = "1 - Order Book Depth Quantity (Put Bid Qty vs Call Ask Qty)";
                peLabel = "Put Bid Qty (Buy Vol)"; ceLabel = "Call Ask Qty (Sell Vol)";
                peColor = 'rgba(34, 197, 94, 0.85)'; ceColor = 'rgba(239, 68, 68, 0.85)';
                peData = rawRows.map(r => r.pe_bid_qty);
                ceData = rawRows.map(r => r.ce_ask_qty);
            }
            else if (selected === "oi_chg") {
                metricTitle = "Change in Open Interest (Chg in OI)";
                peLabel = "Put Chg in OI"; ceLabel = "Call Chg in OI";
                peColor = 'rgba(34, 197, 94, 0.85)'; ceColor = 'rgba(239, 68, 68, 0.85)';
                peData = rawRows.map(r => r.pe_oi_chg);
                ceData = rawRows.map(r => r.ce_oi_chg);
            }
            else if (selected === "oi") {
                metricTitle = "Open Interest (PE vs CE)";
                peLabel = "Put OI"; ceLabel = "Call OI";
                peColor = 'rgba(34, 197, 94, 0.85)'; ceColor = 'rgba(239, 68, 68, 0.85)';
                peData = rawRows.map(r => r.pe_oi);
                ceData = rawRows.map(r => r.ce_oi);
            }
            else if (selected === "vol") {
                metricTitle = "Trading Volume";
                peLabel = "Put Volume"; ceLabel = "Call Volume";
                peColor = 'rgba(34, 197, 94, 0.85)'; ceColor = 'rgba(239, 68, 68, 0.85)';
                peData = rawRows.map(r => r.pe_vol);
                ceData = rawRows.map(r => r.ce_vol);
            }
            else if (selected === "delta") {
                metricTitle = "Option Delta Exposure";
                peLabel = "Put Delta"; ceLabel = "Call Delta";
                peColor = 'rgba(34, 197, 94, 0.85)'; ceColor = 'rgba(239, 68, 68, 0.85)';
                peData = rawRows.map(r => r.pe_delta);
                ceData = rawRows.map(r => r.ce_delta);
            }
            else if (selected === "theta") {
                metricTitle = "Theta Time Decay Rate";
                peLabel = "Put Theta"; ceLabel = "Call Theta";
                peColor = 'rgba(34, 197, 94, 0.85)'; ceColor = 'rgba(239, 68, 68, 0.85)';
                peData = rawRows.map(r => r.pe_theta);
                ceData = rawRows.map(r => r.ce_theta);
            }

            titleEl.innerText = metricTitle;
            myChart.data.labels = strikes;
            myChart.data.datasets[0].label = peLabel;
            myChart.data.datasets[0].data = peData;
            myChart.data.datasets[0].backgroundColor = peColor;
            myChart.data.datasets[0].borderColor = '#22c55e';
            myChart.data.datasets[1].label = ceLabel;
            myChart.data.datasets[1].data = ceData;
            myChart.data.datasets[1].backgroundColor = ceColor;
            myChart.data.datasets[1].borderColor = '#ef4444';
            myChart.data.datasets[2].label = `${peLabel} Trend`;
            myChart.data.datasets[2].data = computeLinearTrend(peData);
            myChart.data.datasets[2].borderColor = '#4ade80';
            myChart.data.datasets[3].label = `${ceLabel} Trend`;
            myChart.data.datasets[3].data = computeLinearTrend(ceData);
            myChart.data.datasets[3].borderColor = '#f87171';
            myChart.update('none');

            const count = rawRows.length || 1;
            const peSum = peData.reduce((a, b) => a + Math.abs(b), 0);
            const ceSum = ceData.reduce((a, b) => a + Math.abs(b), 0);
            const peAvg = (peSum / count).toFixed(2);
            const ceAvg = (ceSum / count).toFixed(2);
            const totalSum = peSum + ceSum;
            const pePct = totalSum > 0 ? ((peSum / totalSum) * 100).toFixed(1) : 50;
            const cePct = totalSum > 0 ? ((ceSum / totalSum) * 100).toFixed(1) : 50;
            const ratio = ceSum > 0 ? (peSum / ceSum).toFixed(2) : "0.00";

            document.getElementById("gaugeMetricName").innerText = metricTitle.toUpperCase();
            document.getElementById("gaugePeLabel").innerText = `PUT (PE) TOTAL: ${peSum.toLocaleString()} (${pePct}%)`;
            document.getElementById("gaugeCeLabel").innerText = `CALL (CE) TOTAL: ${ceSum.toLocaleString()} (${cePct}%)`;
            document.getElementById("gaugePeAvg").innerText = `Put Avg: ${Number(peAvg).toLocaleString()}`;
            document.getElementById("gaugeCeAvg").innerText = `Call Avg: ${Number(ceAvg).toLocaleString()}`;
            document.getElementById("gaugeRatioText").innerText = `Put/Call Ratio: ${ratio}`;
            document.getElementById("gaugePeBar").style.width = `${pePct}%`;
            document.getElementById("gaugeCeBar").style.width = `${cePct}%`;
        }

        let timerSeconds = 10;
        let isSyncing = false;

        function startAutoSync() {
            setInterval(() => {
                if (isSyncing) return;
                timerSeconds--;
                if (timerSeconds <= 0) {
                    timerSeconds = 10;
                    fetchDataInBackground();
                }
                document.getElementById("countdown").innerText = timerSeconds;
            }, 1000);
        }

        function fetchDataInBackground() {
            isSyncing = true;
            document.getElementById("syncBadge").innerText = "Syncing...";

            fetch(`/api/data?symbol=${currentSymbol}&expiry=${currentExpiry}`)
                .then(res => res.json())
                .then(data => {
                    renderPageData(data);
                    loadHistoricalChart(false);
                    loadSignalChart(false);
                })
                .catch(err => console.error("Auto Sync Error:", err))
                .finally(() => {
                    isSyncing = false;
                    document.getElementById("syncBadge").innerHTML = 'Auto Sync: <span id="countdown">' + timerSeconds + '</span>s';
                });
        }

        try { initChart(); } catch(e) { console.error("Bar Chart Init Error:", e); }
        try { initHistoryChart(); } catch(e) { console.error("History Chart Init Error:", e); }
        try { initSignalChart(); } catch(e) { console.error("Signal Chart Init Error:", e); }

        renderPageData({
            rows: rawRows,
            spot_price: {{ spot_price }},
            atm_strike: {{ atm_strike }},
            expiry: "{{ expiry }}",
            updated: "{{ updated }}",
            pcr: {{ pcr }}
        });

        startAutoSync();
    </script>
</body>
</html>
"""

# --- LIVE TRADE & POSITION DASHBOARD TEMPLATE ---
POSITIONS_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Upstox Private Live Trade & Positions Dashboard</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #0b1329; color: #f8fafc; }
        h2 { color: #38bdf8; margin-bottom: 5px; }
        .subtitle { color: #94a3b8; font-size: 13px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; }
        .timer-badge { background: #0284c7; color: #ffffff; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        
        /* STATIC IP WHITELIST BAR (HIDDEN BY DEFAULT; SHOWN ONLY IF NEEDED) */
        .static-ip-bar {
            background: #131c31;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 12px 18px;
            margin-bottom: 14px;
            display: none;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
        }
        .ip-chip { background: #0f172a; border: 1px solid #334155; padding: 4px 10px; border-radius: 6px; font-family: monospace; font-size: 13px; color: #38bdf8; }
        .ip-ok { color: #34d399; font-weight: bold; font-size: 12px; }
        .ip-warn { color: #fbbf24; font-weight: bold; font-size: 12px; }
        .btn-register-ip {
            background-color: #2563eb;
            color: #ffffff;
            border: none;
            padding: 7px 14px;
            border-radius: 6px;
            font-weight: 700;
            font-size: 12px;
            cursor: pointer;
        }
        .btn-register-ip:hover { opacity: 0.9; }
        .btn-register-ip:disabled { opacity: 0.5; cursor: not-allowed; }

        /* TOP MANUAL/AUTO CONDITION BAR */
        .condition-bar {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 14px 18px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
        }
        .badge-mode-auto { background: #065f46; color: #34d399; padding: 5px 12px; border-radius: 6px; font-weight: bold; font-size: 13px; border: 1px solid #10b981; }
        .badge-mode-manual { background: #78350f; color: #fbbf24; padding: 5px 12px; border-radius: 6px; font-weight: bold; font-size: 13px; border: 1px solid #f59e0b; }
        .badge-want-yes { background: #1e3a8a; color: #60a5fa; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 12px; }
        .badge-want-no { background: #334155; color: #94a3b8; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 12px; }
        .badge-active-ok { background: #064e3b; color: #6ee7b7; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 12px; border: 1px solid #10b981; }
        .badge-active-full { background: #7f1d1d; color: #fca5a5; padding: 5px 10px; border-radius: 6px; font-weight: bold; font-size: 12px; border: 1px solid #ef4444; }

        /* MANUAL ORDER APPROVAL PANEL */
        .order-approval-box {
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border: 2px solid #f59e0b;
            border-radius: 12px;
            padding: 18px 22px;
            margin-bottom: 24px;
            box-shadow: 0 8px 20px rgba(0, 0, 0, 0.4);
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 20px;
            flex-wrap: wrap;
        }
        .order-details-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
            gap: 12px;
            flex: 1;
            min-width: 300px;
        }
        .order-field {
            background: #0f172a;
            padding: 8px 12px;
            border-radius: 6px;
            border: 1px solid #334155;
        }
        .order-field-label { font-size: 11px; color: #94a3b8; text-transform: uppercase; font-weight: 600; }
        .order-field-val { font-size: 14px; font-weight: bold; color: #f8fafc; margin-top: 2px; }
        
        .order-actions-corner {
            display: flex;
            flex-direction: column;
            gap: 10px;
            min-width: 150px;
            align-items: stretch;
        }
        .btn-place-order {
            background-color: #22c55e;
            color: #052e16;
            border: none;
            padding: 12px 20px;
            border-radius: 8px;
            font-weight: 800;
            font-size: 14px;
            cursor: pointer;
            text-align: center;
            transition: opacity 0.2s;
        }
        .btn-place-order:hover { opacity: 0.9; }
        .btn-place-order:disabled { opacity: 0.45; cursor: not-allowed; background-color: #475569; color: #94a3b8; }
        .btn-cancel-order {
            background-color: #ef4444;
            color: #ffffff;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            font-weight: 700;
            font-size: 13px;
            cursor: pointer;
            text-align: center;
            transition: opacity 0.2s;
        }
        .btn-cancel-order:hover { opacity: 0.9; }
        .btn-reset-preview {
            background-color: #334155;
            color: #e2e8f0;
            border: 1px solid #475569;
            padding: 5px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }

        .toast-banner {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 18px;
            font-weight: 600;
            font-size: 13px;
            display: none;
        }
        .toast-success { background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }
        .toast-error { background: #450a0a; color: #fca5a5; border: 1px solid #ef4444; }
        .toast-info { background: #1e3a8a; color: #93c5fd; border: 1px solid #3b82f6; }

        .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 18px; margin-bottom: 25px; }
        .card { background: #1e293b; padding: 18px; border-radius: 10px; border-left: 5px solid #3b82f6; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
        .card-title { font-size: 12px; color: #94a3b8; text-transform: uppercase; font-weight: bold; margin-bottom: 6px; }
        .card-value { font-size: 24px; font-weight: bold; }
        
        .pnl-green { color: #22c55e !important; border-left-color: #22c55e !important; }
        .pnl-red { color: #ef4444 !important; border-left-color: #ef4444 !important; }
        
        .section-title { font-size: 18px; font-weight: bold; color: #f8fafc; margin-top: 25px; margin-bottom: 12px; border-left: 4px solid #38bdf8; padding-left: 10px; }
        
        .table-container { overflow-x: auto; background: #1e293b; border-radius: 10px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); margin-bottom: 25px; }
        table { width: 100%; border-collapse: collapse; min-width: 900px; }
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
        <span>Real-time Upstox Portfolio, Automated Google Sheet Order Engine & Executed Log</span>
        <span class="timer-badge" id="syncBadge">Auto Sync: <span id="countdown">10</span>s</span>
    </div>

    <!-- UPSTOX STATIC IP STATUS & WHITELIST BAR (HIDDEN UNLESS ?show_ip=1 OR 403 ERROR) -->
    <div id="staticIpBar" class="static-ip-bar">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
            <span style="font-weight: bold; color: #e2e8f0; font-size: 13px;">🌐 Upstox Static IP Check:</span>
            <span style="font-size: 12px; color: #94a3b8;">Server Outbound IP:</span>
            <span id="txtOutboundIp" class="ip-chip">Checking...</span>
            <span style="font-size: 12px; color: #94a3b8;">Upstox Registered IP:</span>
            <span id="txtRegisteredIp" class="ip-chip">Checking...</span>
            <span id="txtIpMatchStatus" class="ip-warn">Verifying...</span>
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
            <button id="btnRegisterServerIp" class="btn-register-ip" onclick="registerCurrentServerIp()">⚡ Register Server IP with Upstox</button>
        </div>
    </div>

    <!-- TOP MANUAL ORDER CONDITION BAR (B16) -->
    <div class="condition-bar">
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
            <span style="font-weight: bold; color: #e2e8f0; font-size: 14px;">Manual Order Condition (B16):</span>
            <span id="badgeManualCondition" class="badge-mode-manual">LOADING...</span>
            <span id="badgeWantToTrade" class="badge-want-no">Want to Trade (B17): -</span>
            <span id="badgeActiveTrades" class="badge-active-ok">Active Trades (B28): -</span>
            <span id="badgeMaxInvest" style="color: #94a3b8; font-size: 13px; font-weight: 600;">Max Invest (B18): -</span>
            <span id="badgeSheetSyncTime" style="color: #64748b; font-size: 12px;">Last Sheet Sync: -</span>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
            <span id="engineStatusText" style="font-size: 13px; color: #38bdf8; font-weight: 600;">Checking DASHBOARD sheet...</span>
            <button id="btnResetPreview" class="btn-reset-preview" style="display: none;" onclick="resetCancelledPreview()">🔄 Reload Cancelled Order</button>
        </div>
    </div>

    <!-- EXECUTION RESULT / NOTIFICATION BANNER -->
    <div id="orderToastBanner" class="toast-banner"></div>

    <!-- MANUAL ORDER APPROVAL PANEL (SHOWN WHEN B16 = MANUAL AND B17 = YES) -->
    <div id="manualOrderPanel" class="order-approval-box" style="display: none;">
        <div style="flex: 1;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap;">
                <span style="font-size: 16px; font-weight: 800; color: #fbbf24;">📋 Pending Manual Order Approval (DASHBOARD B16:B28)</span>
                <span id="manualGuardWarningBadge" style="display: none; background: #7f1d1d; color: #fca5a5; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: bold;">⚠️ Blocked by Active/Duplicate Trade Guard</span>
            </div>
            <div class="order-details-grid">
                <div class="order-field">
                    <div class="order-field-label">Symbol & Type</div>
                    <div class="order-field-val" id="moSymbolType">-</div>
                </div>
                <div class="order-field">
                    <div class="order-field-label">Expiry (Month → Date)</div>
                    <div class="order-field-val" id="moExpiry">-</div>
                </div>
                <div class="order-field">
                    <div class="order-field-label">Strike Price</div>
                    <div class="order-field-val" id="moStrike">-</div>
                </div>
                <div class="order-field">
                    <div class="order-field-label">Action & Qty</div>
                    <div class="order-field-val" id="moActionQty">-</div>
                </div>
                <div class="order-field">
                    <div class="order-field-label">Product Mode</div>
                    <div class="order-field-val" id="moProduct">-</div>
                </div>
                <div class="order-field">
                    <div class="order-field-label">Live Limit Price (LTP)</div>
                    <div class="order-field-val" style="color: #38bdf8;" id="moEntryPrice">-</div>
                </div>
                <div class="order-field">
                    <div class="order-field-label">Total Investment</div>
                    <div class="order-field-val" style="color: #facc15;" id="moTotalInvest">-</div>
                </div>
                <div class="order-field">
                    <div class="order-field-label">Stop Loss (B26)</div>
                    <div class="order-field-val" style="color: #f87171;" id="moStopLoss">-</div>
                </div>
                <div class="order-field">
                    <div class="order-field-label">Profit Target (B27)</div>
                    <div class="order-field-val" style="color: #4ade80;" id="moTarget">-</div>
                </div>
            </div>
        </div>

        <!-- FAR RIGHT CORNER ACTION BUTTONS -->
        <div class="order-actions-corner">
            <button id="btnPlaceOrder" class="btn-place-order" onclick="executeManualOrder()">✅ Place Order</button>
            <button id="btnCancelOrder" class="btn-cancel-order" onclick="cancelManualOrder()">❌ Cancel</button>
        </div>
    </div>

    <div id="errorContainer" class="error-msg" style="display: none;"></div>

    <div class="metrics-grid">
        <div class="card" id="cardTotalPnlBox">
            <div class="card-title">Total Overall P&L</div>
            <div class="card-value" id="valTotalPnl">₹ 0.00</div>
        </div>
        <div class="card" id="cardUnrealisedPnlBox">
            <div class="card-title">Unrealised P&L (Live Positions)</div>
            <div class="card-value" id="valUnrealisedPnl">₹ 0.00</div>
        </div>
        <div class="card" id="cardRealisedPnlBox">
            <div class="card-title">Realised P&L (Booked Today)</div>
            <div class="card-value" id="valRealisedPnl">₹ 0.00</div>
        </div>
    </div>

    <div class="section-title">📊 Live & Closed Positions</div>
    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th>Status</th>
                    <th>Script / Trading Symbol</th>
                    <th>Product</th>
                    <th>Qty</th>
                    <th>Avg Buy Price</th>
                    <th>Avg Sell Price</th>
                    <th>LTP</th>
                    <th>P&L (₹)</th>
                </tr>
            </thead>
            <tbody id="positionsBody">
                <tr><td colspan="8" style="color: #94a3b8;">Loading positions...</td></tr>
            </tbody>
        </table>
    </div>

    <div class="section-title">📋 Today's Executed Trades & Orders Log</div>
    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th>Order Time</th>
                    <th>Trading Symbol</th>
                    <th>Type</th>
                    <th>Qty</th>
                    <th>Executed Price</th>
                    <th>Status</th>
                    <th>Order ID</th>
                </tr>
            </thead>
            <tbody id="ordersBody">
                <tr><td colspan="7" style="color: #94a3b8;">Loading order log...</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        let timerSeconds = 10;
        let isSyncing = false;
        let latestEvaluatedOrder = null;
        let detectedOutboundIp = "";

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
                .then(res => res.json())
                .then(data => {
                    detectedOutboundIp = data.outbound_ip || "";
                    document.getElementById("txtOutboundIp").innerText = detectedOutboundIp || "Unknown";
                    const regText = data.primary_ip ? (data.secondary_ip ? `${data.primary_ip} / ${data.secondary_ip}` : data.primary_ip) : "Not Configured";
                    document.getElementById("txtRegisteredIp").innerText = regText;

                    const statusEl = document.getElementById("txtIpMatchStatus");
                    if (data.is_matched) {
                        statusEl.className = "ip-ok";
                        statusEl.innerText = "✅ Static IP Matched (Orders Enabled)";
                    } else if (data.error) {
                        statusEl.className = "ip-warn";
                        statusEl.innerText = "⚠️ " + data.error;
                    } else {
                        statusEl.className = "ip-warn";
                        statusEl.innerText = "⚠️ IP Mismatch — Click Register Server IP (1 change/week limit)";
                    }
                })
                .catch(err => console.error("Static IP check error:", err));
        }

        function registerCurrentServerIp() {
            if (!detectedOutboundIp) {
                showToast("❌ Server outbound IP could not be detected yet.", "error");
                return;
            }
            if (!confirm(`Register '${detectedOutboundIp}' as your Upstox Primary Static IP?\\n\\nNote: Upstox allows changing Static IP only once per calendar week.`)) {
                return;
            }
            const btn = document.getElementById("btnRegisterServerIp");
            btn.disabled = true;
            btn.innerText = "Registering IP...";

            fetch('/api/static_ip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ primary_ip: detectedOutboundIp })
            })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    showToast("✅ " + data.message, "success");
                } else {
                    showToast("❌ " + data.message, "error");
                }
                checkStaticIpStatus();
                fetchPositionsInBackground();
            })
            .catch(err => showToast("❌ Request failed: " + err, "error"))
            .finally(() => {
                btn.disabled = false;
                btn.innerText = "⚡ Register Server IP with Upstox";
            });
        }

        function updateCardPnl(elementId, cardId, value) {
            const el = document.getElementById(elementId);
            const card = document.getElementById(cardId);
            const num = parseFloat(value) || 0;
            const formatted = num.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            
            el.innerText = `₹ ${formatted}`;
            if (num >= 0) {
                card.className = "card pnl-green";
            } else {
                card.className = "card pnl-red";
            }
        }

        function renderOrderEngineState(orderState) {
            if (!orderState) return;

            const modeBadge = document.getElementById("badgeManualCondition");
            const wantBadge = document.getElementById("badgeWantToTrade");
            const activeBadge = document.getElementById("badgeActiveTrades");
            const investBadge = document.getElementById("badgeMaxInvest");
            const syncTimeBadge = document.getElementById("badgeSheetSyncTime");
            const statusText = document.getElementById("engineStatusText");
            const manualPanel = document.getElementById("manualOrderPanel");
            const resetBtn = document.getElementById("btnResetPreview");

            const mode = orderState.manual_order || "Manual";
            modeBadge.innerText = mode.toUpperCase();
            modeBadge.className = (mode.toLowerCase() === "auto") ? "badge-mode-auto" : "badge-mode-manual";

            const want = orderState.want_to_trade || "NO";
            wantBadge.innerText = `Want to Trade (B17): ${want}`;
            wantBadge.className = (want === "YES") ? "badge-want-yes" : "badge-want-no";

            const curAct = orderState.active_trade_count ?? 0;
            const maxAct = orderState.max_active_trades ?? 1;
            activeBadge.innerText = `Active Trades (B28): ${curAct} / ${maxAct}`;
            activeBadge.className = (curAct >= maxAct) ? "badge-active-full" : "badge-active-ok";

            const maxInv = orderState.amount_to_invest || 0;
            investBadge.innerText = `Max Invest (B18): ₹${Number(maxInv).toLocaleString('en-IN')}`;
            if (orderState.sheet_synced_at) {
                syncTimeBadge.innerText = `Last Sheet Sync: ${orderState.sheet_synced_at}`;
            }
            statusText.innerText = orderState.status_message || "";

            if (orderState.show_static_ip_bar) {
                revealStaticIpBarIfNeeded(true);
            }

            if (orderState.auto_execution_notice) {
                showToast(orderState.auto_execution_notice, orderState.auto_execution_ok ? "success" : "error");
            }

            if (orderState.is_cancelled) {
                resetBtn.style.display = "inline-block";
            } else {
                resetBtn.style.display = "none";
            }

            if (orderState.show_manual_panel && orderState.evaluated_order) {
                const ev = orderState.evaluated_order;
                latestEvaluatedOrder = ev;
                manualPanel.style.display = "flex";

                document.getElementById("moSymbolType").innerText = `${ev.symbol} (${ev.option_type})`;
                document.getElementById("moExpiry").innerText = `${ev.expiry_month} → ${ev.expiry_date}`;
                
                if (ev.strike_adjusted) {
                    document.getElementById("moStrike").innerHTML = `${ev.selected_strike} <span style="color:#fbbf24; font-size:11px;">(Req: ${ev.requested_strike})</span>`;
                } else {
                    document.getElementById("moStrike").innerText = `${ev.selected_strike}`;
                }

                document.getElementById("moActionQty").innerText = `${ev.transaction_type} | Qty: ${ev.quantity}`;
                document.getElementById("moProduct").innerText = ev.product_label;
                document.getElementById("moEntryPrice").innerText = `₹ ${ev.entry_price}`;
                document.getElementById("moTotalInvest").innerText = `₹ ${Number(ev.total_investment).toLocaleString('en-IN')}`;
                
                document.getElementById("moStopLoss").innerText = (ev.sl_price !== null && ev.sl_price !== undefined)
                    ? `₹ ${ev.sl_price} (${ev.sl_percent}%)`
                    : "Blank (Self-Monitor)";
                
                document.getElementById("moTarget").innerText = (ev.target_price !== null && ev.target_price !== undefined)
                    ? `₹ ${ev.target_price} (${ev.target_percent}%)`
                    : "Blank (Self-Monitor)";

                const warnBadge = document.getElementById("manualGuardWarningBadge");
                const placeBtn = document.getElementById("btnPlaceOrder");
                if (orderState.guard_blocked) {
                    warnBadge.style.display = "inline-block";
                    warnBadge.innerText = "⚠️ " + (orderState.guard_reason || "Blocked by Active/Duplicate Trade Guard");
                    placeBtn.disabled = true;
                    placeBtn.innerText = "🚫 Order Blocked";
                } else {
                    warnBadge.style.display = "none";
                    placeBtn.disabled = false;
                    placeBtn.innerText = "✅ Place Order";
                }
            } else {
                manualPanel.style.display = "none";
            }
        }

        function executeManualOrder() {
            const btn = document.getElementById("btnPlaceOrder");
            btn.disabled = true;
            btn.innerText = "Placing Order...";

            fetch('/api/manual_order_action', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'PLACE', evaluated_order: latestEvaluatedOrder })
            })
            .then(res => res.json())
            .then(data => {
                if (data.show_static_ip_bar) {
                    revealStaticIpBarIfNeeded(true);
                }
                if (data.success) {
                    showToast("✅ " + data.message, "success");
                } else {
                    showToast("❌ " + data.message, "error");
                }
                fetchPositionsInBackground();
            })
            .catch(err => {
                showToast("❌ Order Request Failed: " + err, "error");
            })
            .finally(() => {
                btn.disabled = false;
                btn.innerText = "✅ Place Order";
            });
        }

        function cancelManualOrder() {
            fetch('/api/manual_order_action', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'CANCEL', signature: latestEvaluatedOrder ? latestEvaluatedOrder.signature : "" })
            })
            .then(res => res.json())
            .then(data => {
                showToast("🚫 " + data.message, "info");
                document.getElementById("manualOrderPanel").style.display = "none";
                fetchPositionsInBackground();
            })
            .catch(err => console.error("Cancel error:", err));
        }

        function resetCancelledPreview() {
            fetch('/api/manual_order_action', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'RESET' })
            })
            .then(res => res.json())
            .then(() => {
                fetchPositionsInBackground();
            });
        }

        function fetchPositionsInBackground() {
            isSyncing = true;
            document.getElementById("syncBadge").innerText = "Syncing...";

            fetch('/api/positions?_t=' + Date.now(), { cache: 'no-store' })
                .then(res => res.json())
                .then(data => {
                    const errEl = document.getElementById("errorContainer");
                    if (data.error) {
                        errEl.style.display = "block";
                        errEl.innerText = "Error Fetching Positions: " + data.error;
                    } else {
                        errEl.style.display = "none";
                    }

                    if (data.order_engine) {
                        renderOrderEngineState(data.order_engine);
                    }

                    updateCardPnl("valTotalPnl", "cardTotalPnlBox", data.total_pnl);
                    updateCardPnl("valUnrealisedPnl", "cardUnrealisedPnlBox", data.unrealised_pnl);
                    updateCardPnl("valRealisedPnl", "cardRealisedPnlBox", data.realised_pnl);

                    const posBody = document.getElementById("positionsBody");
                    if (data.positions && data.positions.length > 0) {
                        let html = "";
                        data.positions.forEach(p => {
                            const pnlNum = parseFloat(p.pnl) || 0;
                            const pnlColor = pnlNum >= 0 ? '#22c55e' : '#ef4444';
                            const badge = p.status === 'OPEN' ? '<span class="status-open">OPEN</span>' : '<span class="status-closed">CLOSED</span>';
                            const formattedPnl = pnlNum.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

                            html += `<tr>
                                <td>${badge}</td>
                                <td><strong>${p.trading_symbol}</strong></td>
                                <td>${p.product}</td>
                                <td><strong>${p.quantity}</strong></td>
                                <td>₹ ${p.buy_price}</td>
                                <td>₹ ${p.sell_price}</td>
                                <td>₹ ${p.ltp}</td>
                                <td style="font-weight: bold; color: ${pnlColor};">₹ ${formattedPnl}</td>
                            </tr>`;
                        });
                        posBody.innerHTML = html;
                    } else {
                        posBody.innerHTML = `<tr><td colspan="8" style="color: #94a3b8;">No open or closed positions found for today.</td></tr>`;
                    }

                    const ordBody = document.getElementById("ordersBody");
                    if (data.orders && data.orders.length > 0) {
                        let html = "";
                        data.orders.forEach(o => {
                            const typeColor = o.transaction_type === 'BUY' ? '#38bdf8' : '#f43f5e';
                            html += `<tr>
                                <td style="font-size: 12px; color: #94a3b8;">${o.order_timestamp}</td>
                                <td><strong>${o.trading_symbol}</strong></td>
                                <td style="font-weight: bold; color: ${typeColor};">${o.transaction_type}</td>
                                <td>${o.quantity}</td>
                                <td>₹ ${o.average_price || o.price}</td>
                                <td><strong>${o.status}</strong></td>
                                <td style="font-size: 11px; color: #64748b;">${o.order_id}</td>
                            </tr>`;
                        });
                        ordBody.innerHTML = html;
                    } else {
                        ordBody.innerHTML = `<tr><td colspan="7" style="color: #94a3b8;">No trades executed today.</td></tr>`;
                    }
                })
                .catch(err => console.error("Position sync error:", err))
                .finally(() => {
                    isSyncing = false;
                    document.getElementById("syncBadge").innerHTML = 'Auto Sync: <span id="countdown">' + timerSeconds + '</span>s';
                });
        }

        function startTimer() {
            setInterval(() => {
                if (isSyncing) return;
                timerSeconds--;
                if (timerSeconds <= 0) {
                    timerSeconds = 10;
                    fetchPositionsInBackground();
                }
                document.getElementById("countdown").innerText = timerSeconds;
            }, 1000);
        }

        revealStaticIpBarIfNeeded(false);
        fetchPositionsInBackground();
        startTimer();
    </script>
</body>
</html>
"""

# --- ROUTES ---
@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/static_ip", methods=["GET", "POST"])
def api_static_ip():
    """GET checks outbound IP vs Upstox registered Static IP; POST registers Primary/Secondary Static IP."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        primary_ip = data.get("primary_ip") or get_current_outbound_ip()
        secondary_ip = data.get("secondary_ip")
        ok, msg = update_upstox_static_ip(primary_ip, secondary_ip)
        return jsonify({"success": ok, "message": msg})

    status = fetch_upstox_static_ip_status()
    resp = jsonify(status)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/manual_order_action", methods=["POST"])
def api_manual_order_action():
    """Handles Place Order, Cancel, or Reset from /Live_Trade_Position when B16 = Manual."""
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).upper()

    with ORDER_EXECUTION_LOCK:
        state = load_auto_order_state()

        if action == "CANCEL":
            sig = data.get("signature", "")
            if sig and sig not in state.get("cancelled_signatures", []):
                state.setdefault("cancelled_signatures", []).append(sig)
            save_auto_order_state(state)
            return jsonify({"success": True, "message": "Manual order preview cancelled."})

        if action == "RESET":
            state["cancelled_signatures"] = []
            save_auto_order_state(state)
            return jsonify({"success": True, "message": "Cancelled state cleared."})

        if action == "PLACE":
            cfg, cfg_err = fetch_dashboard_order_config()
            if cfg_err or not cfg:
                return jsonify({"success": False, "message": cfg_err or "Could not read DASHBOARD sheet."})

            positions, orders, gtt_orders, _, _, _, _ = fetch_upstox_positions()
            guard = evaluate_trade_guards(
                cfg["symbol"],
                cfg["option_type"],
                cfg["max_active_trades"],
                positions,
                orders,
                gtt_orders
            )
            if not guard["allowed"]:
                return jsonify({"success": False, "message": guard["reason"]})

            evaluated, eval_err = prepare_evaluated_order(cfg)
            if eval_err or not evaluated:
                return jsonify({"success": False, "message": eval_err or "Order evaluation failed."})

            ok, msg, is_ip_err = submit_upstox_evaluated_order(evaluated)
            if ok:
                state.setdefault("recent_submissions", {})[evaluated["signature"]] = time.time()
                save_auto_order_state(state)
            return jsonify({
                "success": ok,
                "message": msg,
                "show_static_ip_bar": is_ip_err,
                "evaluated_order": evaluated
            })

    return jsonify({"success": False, "message": "Invalid action."})

@app.route("/api/positions")
def api_positions():
    """Background API route fetching live Upstox positions, order log, and DASHBOARD order engine every 10s."""
    positions, orders, gtt_orders, total_pnl, realised_pnl, unrealised_pnl, error = fetch_upstox_positions()

    order_engine_payload = {
        "manual_order": "Manual",
        "want_to_trade": "NO",
        "amount_to_invest": 0,
        "max_active_trades": 1,
        "active_trade_count": 0,
        "sheet_synced_at": get_ist_now().strftime("%H:%M:%S IST"),
        "status_message": "Initializing...",
        "show_manual_panel": False,
        "evaluated_order": None,
        "guard_blocked": False,
        "guard_reason": None,
        "is_cancelled": False,
        "show_static_ip_bar": False,
        "auto_execution_notice": None,
        "auto_execution_ok": False
    }

    with ORDER_EXECUTION_LOCK:
        cfg, cfg_err = fetch_dashboard_order_config()
        if cfg_err or not cfg:
            order_engine_payload["status_message"] = cfg_err or "Unable to read DASHBOARD sheet."
        else:
            order_engine_payload["manual_order"] = cfg["manual_order"]
            order_engine_payload["want_to_trade"] = cfg["want_to_trade"]
            order_engine_payload["amount_to_invest"] = cfg["amount_to_invest"]
            order_engine_payload["max_active_trades"] = cfg["max_active_trades"]

            guard = evaluate_trade_guards(
                cfg["symbol"],
                cfg["option_type"],
                cfg["max_active_trades"],
                positions,
                orders,
                gtt_orders
            )
            order_engine_payload["active_trade_count"] = guard["active_count"]
            order_engine_payload["guard_blocked"] = not guard["allowed"]
            order_engine_payload["guard_reason"] = guard["reason"] if not guard["allowed"] else None

            if cfg["want_to_trade"] != "YES":
                order_engine_payload["status_message"] = "Trade Avoided — Want to Trade (B17) is not YES."
            else:
                evaluated, eval_err = prepare_evaluated_order(cfg)
                if eval_err or not evaluated:
                    order_engine_payload["status_message"] = eval_err or "Order evaluation skipped."
                else:
                    order_engine_payload["evaluated_order"] = evaluated
                    state = load_auto_order_state()

                    if cfg["manual_order"].lower() == "auto":
                        if not guard["allowed"]:
                            order_engine_payload["status_message"] = f"Skipped: {guard['reason']}"
                        else:
                            last_sub_ts = state.get("recent_submissions", {}).get(evaluated["signature"], 0)
                            if time.time() - last_sub_ts < 25:
                                order_engine_payload["status_message"] = "Order submitted — waiting for Upstox order book sync..."
                            else:
                                ok, exec_msg, is_ip_err = submit_upstox_evaluated_order(evaluated)
                                state.setdefault("recent_submissions", {})[evaluated["signature"]] = time.time()
                                save_auto_order_state(state)
                                order_engine_payload["show_static_ip_bar"] = is_ip_err
                                order_engine_payload["status_message"] = f"Auto Mode: {exec_msg}"
                                order_engine_payload["auto_execution_notice"] = f"AUTO ORDER: {exec_msg}"
                                order_engine_payload["auto_execution_ok"] = ok
                    else:
                        is_cancelled = evaluated["signature"] in state.get("cancelled_signatures", [])
                        order_engine_payload["is_cancelled"] = is_cancelled
                        if is_cancelled:
                            order_engine_payload["show_manual_panel"] = False
                            order_engine_payload["status_message"] = "Manual Order Cancelled by User (Click Reload to restore)."
                        else:
                            order_engine_payload["show_manual_panel"] = True
                            if not guard["allowed"]:
                                order_engine_payload["status_message"] = f"Manual Order Blocked: {guard['reason']}"
                            else:
                                order_engine_payload["status_message"] = f"Awaiting Manual Approval for {evaluated['symbol']} {evaluated['selected_strike']} {evaluated['option_type']} @ ₹{evaluated['entry_price']}"

    response = jsonify({
        "positions": positions,
        "orders": orders,
        "total_pnl": total_pnl,
        "realised_pnl": realised_pnl,
        "unrealised_pnl": unrealised_pnl,
        "order_engine": order_engine_payload,
        "error": error
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@app.route("/Live_Trade_Position", methods=["GET"])
def live_trade_position():
    """Unlisted private route for real-time portfolio, trade logs & P&L."""
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
        if "15" in tf:
            tf_str = "15_MIN"
        elif "30" in tf:
            tf_str = "30_MIN"
        elif "60" in tf or "1h" in tf or "hour" in tf or "hr" in tf or "day" in tf:
            tf_str = "1_HR"
        else:
            tf_str = "5_MIN"
            
        target_sheet = f"{sym_clean}_{tf_str}"
    else:
        target_sheet = sym_clean

    snapshots, err = fetch_history_from_gsheet(symbol, sheet_name=target_sheet)

    if err:
        return jsonify({"history": [], "count": 0, "error": err})

    if not snapshots:
        return jsonify({"history": [], "count": 0})

    day_map = {
        "1day": 1 * 86400,
        "3day": 3 * 86400,
        "1week": 7 * 86400,
        "2week": 14 * 86400,
        "1month": 30 * 86400,
        "3month": 90 * 86400
    }
    max_ts = max((s["timestamp"] for s in snapshots if s["timestamp"] > 0), default=0)

    if max_ts > 0 and day_range in day_map:
        cutoff = max_ts - day_map[day_range]
        snapshots = [s for s in snapshots if s["timestamp"] >= cutoff or s["timestamp"] == 0]

    if timeframe == "5minute" or not snapshots:
        return jsonify({"history": snapshots, "count": len(snapshots)})

    sec_map = {
        "15minute": 15 * 60,
        "30minute": 30 * 60,
        "60minute": 60 * 60,
        "day": 24 * 60 * 60
    }
    interval_sec = sec_map.get(timeframe, 300)

    grouped = {}
    for idx, item in enumerate(snapshots):
        ts = item["timestamp"]
        bucket = (ts // interval_sec) * interval_sec if ts > 0 else (idx // (interval_sec // 300))
        grouped[bucket] = item

    resampled = [grouped[b] for b in sorted(grouped.keys())]
    return jsonify({"history": resampled, "count": len(resampled)})

@app.route("/api/data")
def api_data():
    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry", "ALL").upper()
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)
    
    return jsonify({
        "rows": rows,
        "spot_price": spot_price,
        "atm_strike": atm_strike,
        "expiry": expiry,
        "expiries": expiries,
        "updated": updated,
        "pcr": pcr,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "error": error
    })

@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return Response(status=200)

    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry", "ALL").upper()
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)

    return render_template_string(
        MAIN_HTML_TEMPLATE,
        rows=rows,
        symbol=symbol,
        spot_price=spot_price,
        atm_strike=atm_strike,
        expiry=expiry,
        expiries=expiries,
        updated=updated,
        pcr=pcr,
        total_ce_oi=total_ce_oi,
        total_pe_oi=total_pe_oi,
        error=error
    )

@app.route("/analysis", methods=["GET"])
def analysis_dashboard():
    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry", "ALL").upper()
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)

    return render_template_string(
        ANALYSIS_HTML_TEMPLATE,
        rows=rows,
        symbol=symbol,
        spot_price=spot_price,
        atm_strike=atm_strike,
        expiry=expiry,
        expiries=expiries,
        updated=updated,
        pcr=pcr,
        total_ce_oi=total_ce_oi,
        total_pe_oi=total_pe_oi,
        error=error
    )

@app.route("/download")
def download_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry", "ALL").upper()
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([f"Symbol: {symbol}", f"Spot Price: {spot_price}", f"ATM Strike: {atm_strike}", f"Expiry: {expiry}", f"PCR: {pcr}", f"Updated: {updated}"])
    cw.writerow([
        "CE OI", "CE Chg in OI", "CE Vol", "CE IV", "CE Delta", "CE Theta", "CE Vega", "CE Gamma", "CE Bid", "CE Ask", "CE LTP",
        "STRIKE",
        "PE LTP", "PE Bid", "PE Ask", "PE Delta", "PE Theta", "PE Vega", "PE Gamma", "PE IV", "PE Vol", "PE Chg in OI", "PE OI"
    ])

    if not error:
        for r in rows:
            cw.writerow([
                r["ce_oi"], r["ce_oi_chg"], r["ce_vol"], r["ce_iv"], r["ce_delta"], r["ce_theta"], r["ce_vega"], r["ce_gamma"], r["ce_bid"], r["ce_ask"], r["ce_ltp"],
                r["strike"],
                r["pe_ltp"], r["pe_bid"], r["pe_ask"], r["pe_delta"], r["pe_theta"], r["pe_vega"], r["pe_gamma"], r["pe_iv"], r["pe_vol"], r["pe_oi_chg"], r["pe_oi"]
            ])

    output = Response(si.getvalue(), mimetype="text/csv")
    output.headers["Content-Disposition"] = f"attachment; filename={symbol}_{expiry}_Option_Chain.csv"
    return output

@app.route("/download_history")
def download_history_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    snapshots, _ = fetch_history_from_gsheet(symbol)

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([
        "Datetime", "Formatted Time", "Symbol", "Spot Price", "ATM Strike", "PCR",
        "CE OI", "PE OI", "CE Chg in OI", "PE Chg in OI", "CE Vol", "PE Vol",
        "CE IV", "PE IV", "CE Delta", "PE Delta",
        "CE Theta", "PE Theta", "CE Vega", "PE Vega",
        "CE Gamma", "PE Gamma", "CE Ask", "PE Bid",
        "Min ROC (Buy)", "Max ROC (Sell)", "Cross ROC Sig", "Main Volatility", "New Signal"
    ])

    for s in snapshots:
        cw.writerow([
            s.get("datetime"), s.get("formatted_time"), s.get("symbol"),
            s.get("spot_price"), s.get("atm_strike"), s.get("pcr"),
            s.get("ce_oi"), s.get("pe_oi"), s.get("ce_oi_chg"), s.get("pe_oi_chg"),
            s.get("ce_vol"), s.get("pe_vol"),
            s.get("ce_iv"), s.get("pe_iv"), s.get("ce_delta"), s.get("pe_delta"),
            s.get("ce_theta"), s.get("pe_theta"), s.get("ce_vega"), s.get("pe_vega"),
            s.get("ce_gamma"), s.get("pe_gamma"), s.get("ce_ask"), s.get("pe_bid"),
            s.get("min_roc"), s.get("max_roc"), s.get("cross_roc"), s.get("main_volatility"), s.get("new_signal")
        ])

    output = Response(si.getvalue(), mimetype="text/csv")
    output.headers["Content-Disposition"] = f"attachment; filename={symbol}_Historical_GSheet_Memory.csv"
    return output

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
