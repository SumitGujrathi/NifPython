import os
import time
import io
import csv
import json
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

SESSION_FILE_PATH = "/tmp/upstox_session.json"
AUTH_THREAD_LOCK = threading.Lock()

INSTRUMENT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank"
}

AUTH_COOLDOWN = {
    "last_failure_time": 0,
    "cooldown_duration": 1500  # 25 minutes safety lockout
}

# --- IN-MEMORY HISTORICAL SNAPSHOT STORAGE ---
HISTORICAL_SNAPSHOTS = []  # Buffer for 5-minute snapshot time series
SNAPSHOT_LOCK = threading.Lock()

# --- MARKET HOURS & MEMORY MANAGEMENT ---
def is_market_open():
    """Checks if current IST time is within Indian stock market hours (Mon-Fri, 9:15 AM - 3:30 PM)."""
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_tz)
    if now_ist.weekday() >= 5:
        return False
    start_time = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    end_time = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return start_time <= now_ist <= end_time

def trim_memory_if_needed():
    """Ensures memory usage of snapshot buffer stays under 50 MB, trimming down to ~30 MB."""
    global HISTORICAL_SNAPSHOTS
    with SNAPSHOT_LOCK:
        try:
            raw_bytes = len(json.dumps(HISTORICAL_SNAPSHOTS).encode('utf-8'))
            max_limit = 50 * 1024 * 1024   # 50 MB threshold
            target_limit = 30 * 1024 * 1024  # 30 MB target

            if raw_bytes > max_limit:
                print(f"[Memory Trim] Size {raw_bytes / (1024*1024):.2f} MB exceeds 50MB. Trimming down to ~30MB...")
                while len(HISTORICAL_SNAPSHOTS) > 20:
                    current_size = len(json.dumps(HISTORICAL_SNAPSHOTS).encode('utf-8'))
                    if current_size <= target_limit:
                        break
                    trim_count = max(1, len(HISTORICAL_SNAPSHOTS) // 10)
                    HISTORICAL_SNAPSHOTS = HISTORICAL_SNAPSHOTS[trim_count:]
                print(f"[Memory Trim Complete] New size: {len(json.dumps(HISTORICAL_SNAPSHOTS).encode('utf-8')) / (1024*1024):.2f} MB")
        except Exception as e:
            print(f"[Memory Trim Exception]: {e}")

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

def perform_single_auth():
    with AUTH_THREAD_LOCK:
        existing_token = load_session_token()
        if existing_token:
            return existing_token

        current_time = time.time()
        if current_time - AUTH_COOLDOWN["last_failure_time"] < AUTH_COOLDOWN["cooldown_duration"]:
            remaining = int(AUTH_COOLDOWN["cooldown_duration"] - (current_time - AUTH_COOLDOWN["last_failure_time"]))
            print(f"[Auth Security Lock] Cooldown active! Login blocked for {remaining}s.")
            return None

        if not all([CLIENT_ID, USER_ID, PIN, TOTP_KEY]):
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
            res = requests.post(totp_url, json=payload, headers=headers, timeout=10)

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

# --- TECHNICAL ANALYSIS (EMA CALCULATOR) ---
def calculate_ema(prices, period):
    """Calculates Exponential Moving Average (EMA) for a price series."""
    if len(prices) < period:
        return [None] * len(prices)
    
    ema = [None] * len(prices)
    sma = sum(prices[:period]) / period
    ema[period - 1] = round(sma, 2)
    multiplier = 2 / (period + 1)
    
    for i in range(period, len(prices)):
        val = (prices[i] - ema[i - 1]) * multiplier + ema[i - 1]
        ema[i] = round(val, 2)
    return ema

# --- UPSTOX HISTORICAL & INTRADAY LIVE CANDLE DATA ---
def fetch_historical_candles(symbol, timeframe="5minute", days=60):
    token = get_valid_token()
    if not token:
        return {"candles": [], "ema8": [], "ema24": []}, "Authentication required."

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    ist_tz = timezone(timedelta(hours=5, minutes=30))
    to_date = datetime.now(ist_tz).strftime("%Y-%m-%d")
    from_date = (datetime.now(ist_tz) - timedelta(days=days)).strftime("%Y-%m-%d")

    tf_map = {
        "5minute": "5minute",
        "15minute": "15minute",
        "30minute": "30minute",
        "60minute": "60minute",
        "day": "day"
    }
    interval = tf_map.get(timeframe, "5minute")

    try:
        url = f"https://api.upstox.com/v2/historical-candle/{urllib.parse.quote(instrument_key)}/{interval}/{to_date}/{from_date}"
        res = requests.get(url, headers=headers, timeout=10)

        if res.status_code != 200:
            return {"candles": [], "ema8": [], "ema24": []}, f"Candle API status {res.status_code}: {res.text}"

        raw_candles = res.json().get("data", {}).get("candles", [])
        if not raw_candles:
            return {"candles": [], "ema8": [], "ema24": []}, "No candle data returned."

        raw_candles.reverse()

        seen_times = set()
        parsed_candles = []
        close_prices = []

        for c in raw_candles:
            ts_str = c[0]
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                unix_ts = int(dt.timestamp())
            except Exception:
                continue

            if unix_ts in seen_times:
                continue
            seen_times.add(unix_ts)

            close_p = round(float(c[4]), 2)
            parsed_candles.append({
                "time": unix_ts,
                "open": round(float(c[1]), 2),
                "high": round(float(c[2]), 2),
                "low": round(float(c[3]), 2),
                "close": close_p,
                "volume": int(c[5])
            })
            close_prices.append(close_p)

        parsed_candles.sort(key=lambda x: x["time"])

        ema8_list = calculate_ema(close_prices, 8)
        ema24_list = calculate_ema(close_prices, 24)

        ema8_data = []
        ema24_data = []

        for i in range(len(parsed_candles)):
            t = parsed_candles[i]["time"]
            if ema8_list[i] is not None:
                ema8_data.append({"time": t, "value": ema8_list[i]})
            if ema24_list[i] is not None:
                ema24_data.append({"time": t, "value": ema24_list[i]})

        return {
            "candles": parsed_candles,
            "ema8": ema8_data,
            "ema24": ema24_data
        }, None

    except Exception as e:
        return {"candles": [], "ema8": [], "ema24": []}, f"Exception fetching candles: {e}"

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

def fetch_option_chain(symbol, selected_expiry=None):
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
            token = perform_single_auth()
            if not token:
                return None, None, [], "Session expired and re-authentication paused."
            headers["Authorization"] = f"Bearer {token}"
            exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code != 200:
            return None, None, [], f"Expiry lookup failed: {exp_res.text}"

        expiries = sorted(list(set(item["expiry"] for item in exp_res.json().get("data", []))))
        if not expiries:
            return None, None, [], "No option contract expiries found."

        if selected_expiry == "ALL":
            combined_chain = []
            target_expiries = expiries[:6]
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(fetch_single_expiry_chain, instrument_key, exp, headers) for exp in target_expiries]
                for future in as_completed(futures):
                    _, data = future.result()
                    if data:
                        combined_chain.extend(data)
            
            return combined_chain, "ALL EXPIRIES (CONSOLIDATED)", expiries, None

        target_expiry = selected_expiry if (selected_expiry and selected_expiry in expiries) else expiries[0]
        _, chain_data = fetch_single_expiry_chain(instrument_key, target_expiry, headers)
        return chain_data, target_expiry, expiries, None

    except Exception as e:
        return None, None, [], f"Exception during fetch: {e}"

def process_option_data(symbol, selected_expiry=None):
    chain_data, current_expiry, expiries, error = fetch_option_chain(symbol, selected_expiry)
    
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    updated_time = datetime.now(ist_tz).strftime("%d/%m/%Y %H:%M:%S IST")

    if error or not chain_data:
        return [], 0, 0, current_expiry or "N/A", expiries, updated_time, 0, 0, 0, error or "No option chain data available."

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

# --- BACKGROUND 5-MINUTE SNAPSHOT RECORDER ---
def record_snapshot(symbol):
    """Computes strike-wide aggregated CE/PE metrics & spot price and records a 5-minute snapshot."""
    try:
        rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry="ALL")
        if error or not rows:
            return

        ist_tz = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist_tz)
        ts_unix = int(now_ist.timestamp())
        formatted_ts = now_ist.strftime("%H:%M")
        full_dt_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")

        num_strikes = len(rows) or 1
        ce_vol_sum = sum(r["ce_vol"] for r in rows)
        pe_vol_sum = sum(r["pe_vol"] for r in rows)
        ce_iv_avg = round(sum(r["ce_iv"] for r in rows) / num_strikes, 2)
        pe_iv_avg = round(sum(r["pe_iv"] for r in rows) / num_strikes, 2)
        ce_delta_sum = round(sum(r["ce_delta"] for r in rows), 2)
        pe_delta_sum = round(sum(r["pe_delta"] for r in rows), 2)
        ce_theta_sum = round(sum(r["ce_theta"] for r in rows), 2)
        pe_theta_sum = round(sum(r["pe_theta"] for r in rows), 2)
        ce_vega_sum = round(sum(r["ce_vega"] for r in rows), 2)
        pe_vega_sum = round(sum(r["pe_vega"] for r in rows), 2)
        ce_gamma_sum = round(sum(r["ce_gamma"] for r in rows), 4)
        pe_gamma_sum = round(sum(r["pe_gamma"] for r in rows), 4)
        ce_bid_qty_sum = sum(r["ce_bid_qty"] for r in rows)
        ce_ask_qty_sum = sum(r["ce_ask_qty"] for r in rows)
        pe_bid_qty_sum = sum(r["pe_bid_qty"] for r in rows)
        pe_ask_qty_sum = sum(r["pe_ask_qty"] for r in rows)

        snapshot = {
            "timestamp": ts_unix,
            "datetime": full_dt_str,
            "formatted_time": formatted_ts,
            "symbol": symbol,
            "spot_price": spot_price,
            "atm_strike": atm_strike,
            "pcr": pcr,
            "ce_oi": total_ce_oi,
            "pe_oi": total_pe_oi,
            "ce_vol": ce_vol_sum,
            "pe_vol": pe_vol_sum,
            "ce_iv": ce_iv_avg,
            "pe_iv": pe_iv_avg,
            "ce_delta": ce_delta_sum,
            "pe_delta": pe_delta_sum,
            "ce_theta": ce_theta_sum,
            "pe_theta": pe_theta_sum,
            "ce_vega": ce_vega_sum,
            "pe_vega": pe_vega_sum,
            "ce_gamma": ce_gamma_sum,
            "pe_gamma": pe_gamma_sum,
            "ce_bid_qty": ce_bid_qty_sum,
            "ce_ask_qty": ce_ask_qty_sum,
            "pe_bid_qty": pe_bid_qty_sum,
            "pe_ask_qty": pe_ask_qty_sum
        }

        with SNAPSHOT_LOCK:
            HISTORICAL_SNAPSHOTS.append(snapshot)
            print(f"[Recorder Thread] Recorded snapshot for {symbol} at {formatted_ts}. Total stored: {len(HISTORICAL_SNAPSHOTS)}")
    except Exception as e:
        print(f"[Snapshot Error] Exception recording {symbol}: {e}")

def background_5min_recorder():
    """Background daemon loop that triggers snapshot recording every 5 minutes during market hours."""
    time.sleep(5)
    print("[Recorder Thread] Background 5-minute snapshot thread active.")
    while True:
        try:
            if is_market_open():
                print("[Recorder Thread] Market is OPEN. Executing 5-min snapshot sweep...")
                for sym in ["NIFTY", "BANKNIFTY"]:
                    record_snapshot(sym)
                trim_memory_if_needed()
            else:
                print("[Recorder Thread] Market is CLOSED. Skipping snapshot recording.")
        except Exception as e:
            print(f"[Recorder Thread Exception]: {e}")
        time.sleep(300)

def start_background_recorder():
    thread = threading.Thread(target=background_5min_recorder, daemon=True)
    thread.start()

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
        table { width: 100%; border-collapse: collapse; min-width: 1200px; }
        th, td { border: 1px solid #e9ecef; padding: 7px 9px; text-align: center; font-size: 13px; }
        th { background-color: #343a40; color: white; position: sticky; top: 0; }
        tr:nth-child(even){ background-color: #f8f9fa; }
        
        .ce-header { background-color: #dc3545 !important; color: white; }
        .pe-header { background-color: #28a745 !important; color: white; }
        .ce-cell { background-color: #fff5f5; }
        .pe-cell { background-color: #f0fff4; }
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
            <a href="/?symbol=NIFTY" class="btn">View NIFTY</a>
            <a href="/?symbol=BANKNIFTY" class="btn">View BANKNIFTY</a>
            
            <form method="GET" action="/" style="display: inline-flex; align-items: center; gap: 6px; margin: 0;">
                <input type="hidden" name="symbol" value="{{ symbol }}">
                <label style="font-weight: bold; color: #334155;">Select Expiry:</label>
                <select name="expiry" onchange="this.form.submit()">
                    <option value="ALL" {% if expiry == 'ALL EXPIRIES (CONSOLIDATED)' or expiry == 'ALL' %}selected{% endif %}>★ ALL EXPIRIES (CONSOLIDATED)</option>
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
                    <th colspan="10" class="ce-header">CALL OPTIONS (CE)</th>
                    <th>STRIKE</th>
                    <th colspan="10" class="pe-header">PUT OPTIONS (PE)</th>
                </tr>
                <tr>
                    <th>OI</th><th>Vol</th><th>IV</th><th>Delta</th><th>Theta</th><th>Vega</th><th>Gamma</th><th>Bid</th><th>Ask</th><th>LTP</th>
                    <th>Price</th>
                    <th>LTP</th><th>Bid</th><th>Ask</th><th>Delta</th><th>Theta</th><th>Vega</th><th>Gamma</th><th>IV</th><th>Vol</th><th>OI</th>
                </tr>
            </thead>
            <tbody>
                {% for row in rows %}
                <tr>
                    <td class="ce-cell">{{ "{:,}".format(row.ce_oi) }}</td>
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
    <script src="https://cdn.jsdelivr.net/npm/lightweight-charts@3.8.0/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #0f172a; color: #f8fafc; }
        h2 { color: #38bdf8; margin-bottom: 5px; }
        .subtitle { color: #94a3b8; font-size: 14px; margin-bottom: 20px; }
        .nav-bar { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; align-items: center; background: #1e293b; padding: 15px; border-radius: 10px; }
        .btn { padding: 9px 18px; color: #fff; background-color: #3b82f6; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 14px; border: none; cursor: pointer; }
        .btn-table { background-color: #64748b; }
        .btn:hover { opacity: 0.9; }
        select { padding: 9px 14px; background: #0f172a; color: #38bdf8; border: 1px solid #334155; border-radius: 6px; font-weight: 600; font-size: 14px; outline: none; }
        .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
        .card { background: #1e293b; padding: 16px; border-radius: 10px; border-left: 5px solid #3b82f6; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
        .card-title { font-size: 12px; color: #94a3b8; text-transform: uppercase; font-weight: bold; margin-bottom: 6px; }
        .card-value { font-size: 22px; font-weight: bold; color: #f8fafc; }
        .sentiment-bar-container { background: #1e293b; border: 1px solid #334155; padding: 15px; border-radius: 10px; margin-bottom: 25px; }
        .gauge-flex { display: flex; height: 24px; border-radius: 12px; overflow: hidden; margin-top: 10px; }
        .gauge-ce { background-color: #ef4444; height: 100%; transition: width 0.4s ease; }
        .gauge-pe { background-color: #22c55e; height: 100%; transition: width 0.4s ease; }
        .chart-box { background: #1e293b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); margin-bottom: 30px; }
        .chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; flex-wrap: wrap; gap: 10px; }
        .chart-title { font-size: 18px; font-weight: bold; color: #f8fafc; }
        .timer-badge { background: #0284c7; color: #ffffff; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .legend-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: bold; margin-left: 15px; }
        .error-msg { color: #f87171; font-weight: bold; padding: 15px; background: #450a0a; border-radius: 8px; }
        #candleContainer { width: 100%; height: 440px; border-radius: 8px; overflow: hidden; }
    </style>
</head>
<body>
    <h2>📊 {{ symbol }} Advanced Analytics & Historical Memory Dashboard</h2>
    <div class="subtitle">Live background auto-sync — Real-time memory snapshots, strike positioning, and live candlestick price</div>

    <div class="nav-bar">
        <label style="color: #94a3b8; font-weight: bold;">Symbol:</label>
        <select id="symbolSelector" onchange="switchSymbol(this.value)">
            <option value="NIFTY" {% if symbol == 'NIFTY' %}selected{% endif %}>NIFTY 50</option>
            <option value="BANKNIFTY" {% if symbol == 'BANKNIFTY' %}selected{% endif %}>BANK NIFTY</option>
        </select>

        <label style="color: #94a3b8; font-weight: bold; margin-left: 10px;">Expiry:</label>
        <select id="expirySelector" onchange="switchExpiry(this.value)">
            <option value="ALL" {% if expiry == 'ALL EXPIRIES (CONSOLIDATED)' or expiry == 'ALL' %}selected{% endif %}>★ ALL EXPIRIES (CONSOLIDATED)</option>
            {% for exp in expiries %}
                <option value="{{ exp }}" {% if exp == expiry %}selected{% endif %}>{{ exp }}</option>
            {% endfor %}
        </select>

        <label style="color: #94a3b8; font-weight: bold; margin-left: 10px;">Bar Chart Metric:</label>
        <select id="metricSelector" onchange="updateChartMetric()">
            <option value="oi_chg" selected>Change in Open Interest (Chg in OI)</option>
            <option value="oi">Open Interest (CE vs PE)</option>
            <option value="vol">Trading Volume</option>
            <option value="depth_qty">Order Book Depth (Bid Quantity vs Ask Quantity)</option>
            <option value="delta">Delta Exposure</option>
            <option value="theta">Theta Time Decay</option>
        </select>

        <div style="margin-left: auto; display: flex; align-items: center; gap: 12px;">
            <span class="timer-badge">Auto Sync: <span id="countdown">10</span>s</span>
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

    <!-- DYNAMIC METRIC SENTIMENT GAUGE -->
    <div class="sentiment-bar-container">
        <div style="display: flex; justify-content: space-between; font-weight: bold; margin-bottom: 8px;">
            <span style="color: #ef4444;" id="gaugeCeLabel">CALL (CE) TOTAL: -</span>
            <span style="color: #38bdf8;" id="gaugeMetricName">SELECTED METRIC COMPARISON</span>
            <span style="color: #22c55e;" id="gaugePeLabel">PUT (PE) TOTAL: -</span>
        </div>
        <div class="gauge-flex">
            <div id="gaugeCeBar" class="gauge-ce" style="width: 50%;"></div>
            <div id="gaugePeBar" class="gauge-pe" style="width: 50%;"></div>
        </div>
        <div style="display: flex; justify-content: space-between; color: #94a3b8; font-size: 12px; margin-top: 6px;">
            <span id="gaugeCeAvg">Call Avg: -</span>
            <span id="gaugeRatioText">Ratio: 1.00</span>
            <span id="gaugePeAvg">Put Avg: -</span>
        </div>
    </div>

    <!-- CHART 1: HISTORICAL SNAPSHOT TREND (5-MIN MARKET MEMORY) -->
    <div class="chart-box">
        <div class="chart-header">
            <div style="display: flex; align-items: center; gap: 15px;">
                <span class="chart-title">📈 Historical Snapshot Trend (5-Min Market Memory)</span>
            </div>
            <div style="display: flex; gap: 12px; align-items: center;">
                <label style="color: #94a3b8; font-weight: bold;">Trend Metric:</label>
                <select id="historyMetricSelector" onchange="loadHistoricalChart()">
                    <option value="spot_price">Live Spot Price (Index)</option>
                    <option value="oi" selected>Open Interest (CE vs PE)</option>
                    <option value="vol">Trading Volume (CE vs PE)</option>
                    <option value="pcr">Put-Call Ratio (PCR)</option>
                    <option value="iv">Implied Volatility (IV CE vs PE)</option>
                    <option value="delta">Delta Exposure (CE vs PE)</option>
                    <option value="theta">Theta Decay (CE vs PE)</option>
                    <option value="vega">Vega Exposure (CE vs PE)</option>
                    <option value="gamma">Gamma Exposure (CE vs PE)</option>
                    <option value="depth_qty">Order Book Depth (Ask Qty vs Bid Qty)</option>
                </select>

                <label style="color: #94a3b8; font-weight: bold; margin-left: 10px;">Interval:</label>
                <select id="historyTimeframeSelector" onchange="loadHistoricalChart()">
                    <option value="5minute" selected>5 Min</option>
                    <option value="15minute">15 Min</option>
                    <option value="30minute">30 Min</option>
                    <option value="60minute">1 Hour</option>
                    <option value="day">1 Day</option>
                </select>
            </div>
        </div>
        <canvas id="historyChart" style="max-height: 420px;"></canvas>
    </div>

    <!-- CHART 2: STRIKE-WISE OPTION CHAIN BAR CHART -->
    <div class="chart-box">
        <div class="chart-header">
            <div class="chart-title" id="chartHeaderTitle">Change in Open Interest (Chg in OI)</div>
            <span style="color: #facc15; font-size: 13px; font-weight: bold;">★ Yellow Line = Spot ATM Strike | Dashed Lines = Linear Trendlines</span>
        </div>
        <canvas id="analysisChart" style="max-height: 480px;"></canvas>
    </div>

    <!-- CHART 3: TECHNICAL PRICE CHART (CANDLESTICK & EMAs) -->
    <div class="chart-box">
        <div class="chart-header">
            <div style="display: flex; align-items: center; gap: 15px;">
                <span class="chart-title">📊 Technical Price Chart (Candlestick & EMAs - Live Today)</span>
                <span class="legend-badge" style="color: #facc15;">🟡 8 EMA</span>
                <span class="legend-badge" style="color: #38bdf8;">🔵 24 EMA</span>
            </div>
            <div>
                <label style="color: #94a3b8; font-weight: bold; margin-right: 8px;">Chart Timeframe:</label>
                <select id="candleTimeframeSelector" onchange="loadCandleChart()">
                    <option value="5minute" selected>5 Min</option>
                    <option value="15minute">15 Min</option>
                    <option value="30minute">30 Min</option>
                    <option value="60minute">1 Hour</option>
                    <option value="day">1 Day</option>
                </select>
            </div>
        </div>
        <div id="candleContainer"></div>
    </div>

    <script>
        let currentSymbol = "{{ symbol }}";
        let currentExpiry = "{{ expiry }}";
        let rawRows = {{ rows | tojson }};
        let currentAtmStrike = {{ atm_strike }};
        let myChart = null;
        let historyChartObj = null;

        // Lightweight Charts References
        let tvChart = null;
        let candleSeries = null;
        let ema8Series = null;
        let ema24Series = null;

        function switchSymbol(newSym) {
            window.location.href = `/analysis?symbol=${newSym}`;
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

        // --- TRADINGVIEW LIGHTWEIGHT CHARTS INITIALIZATION ---
        function initCandleChart() {
            const container = document.getElementById('candleContainer');
            if (!container || typeof LightweightCharts === 'undefined') return;

            tvChart = LightweightCharts.createChart(container, {
                layout: {
                    backgroundColor: '#0f172a',
                    textColor: '#94a3b8'
                },
                grid: {
                    vertLines: { color: '#1e293b' },
                    horzLines: { color: '#1e293b' }
                },
                crosshair: {
                    mode: LightweightCharts.CrosshairMode.Normal
                },
                rightPriceScale: {
                    borderColor: '#334155'
                },
                timeScale: {
                    borderColor: '#334155',
                    timeVisible: true,
                    secondsVisible: false
                }
            });

            candleSeries = tvChart.addCandlestickSeries({
                upColor: '#22c55e',
                downColor: '#ef4444',
                borderVisible: false,
                wickUpColor: '#22c55e',
                wickDownColor: '#ef4444'
            });

            ema8Series = tvChart.addLineSeries({
                color: '#facc15',
                lineWidth: 2,
                title: '8 EMA'
            });

            ema24Series = tvChart.addLineSeries({
                color: '#38bdf8',
                lineWidth: 2,
                title: '24 EMA'
            });

            loadCandleChart();
        }

        function loadCandleChart() {
            const tf = document.getElementById('candleTimeframeSelector').value;
            fetch(`/api/candles?symbol=${currentSymbol}&timeframe=${tf}`)
                .then(res => res.json())
                .then(data => {
                    if (data.error || !data.candles || data.candles.length === 0) return;
                    try {
                        if (candleSeries) candleSeries.setData(data.candles);
                        if (ema8Series && data.ema8) ema8Series.setData(data.ema8);
                        if (ema24Series && data.ema24) ema24Series.setData(data.ema24);
                        if (tvChart) tvChart.timeScale().fitContent();
                    } catch (e) {
                        console.error("Error populating candle series:", e);
                    }
                })
                .catch(err => console.error("Error fetching candle data:", err));
        }

        // --- HISTORICAL SNAPSHOT TREND CHART (FROM MEMORY) ---
        function initHistoryChart() {
            const ctx = document.getElementById('historyChart').getContext('2d');
            historyChartObj = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: []
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
                        y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } }
                    },
                    plugins: {
                        legend: { labels: { color: '#f8fafc', font: { size: 13 } } },
                        tooltip: { mode: 'index', intersect: false }
                    }
                }
            });
            loadHistoricalChart();
        }

        function loadHistoricalChart() {
            const metric = document.getElementById('historyMetricSelector').value;
            const timeframe = document.getElementById('historyTimeframeSelector').value;

            fetch(`/api/history?symbol=${currentSymbol}&timeframe=${timeframe}&metric=${metric}`)
                .then(res => res.json())
                .then(data => {
                    const snapshots = data.history || [];
                    const times = snapshots.map(s => s.formatted_time || s.datetime);

                    let datasets = [];

                    if (metric === "spot_price") {
                        const spotVals = snapshots.map(s => s.spot_price);
                        datasets.push({
                            label: 'Spot Price',
                            data: spotVals,
                            borderColor: '#facc15',
                            backgroundColor: 'rgba(250, 204, 21, 0.1)',
                            borderWidth: 2,
                            tension: 0.2,
                            fill: true
                        });
                        datasets.push({
                            label: 'Spot Linear Trend',
                            data: computeLinearTrend(spotVals),
                            borderColor: '#ca8a04',
                            borderDash: [6, 4],
                            borderWidth: 2,
                            pointRadius: 0,
                            fill: false
                        });
                    }
                    else if (metric === "pcr") {
                        const pcrVals = snapshots.map(s => s.pcr);
                        datasets.push({
                            label: 'Put-Call Ratio (PCR)',
                            data: pcrVals,
                            borderColor: '#38bdf8',
                            borderWidth: 2,
                            tension: 0.2
                        });
                        datasets.push({
                            label: 'PCR Linear Trend',
                            data: computeLinearTrend(pcrVals),
                            borderColor: '#0284c7',
                            borderDash: [6, 4],
                            borderWidth: 2,
                            pointRadius: 0,
                            fill: false
                        });
                    }
                    else {
                        let keyCe = `ce_${metric}`, keyPe = `pe_${metric}`;
                        let titleCe = `Call (CE) ${metric.toUpperCase()}`, titlePe = `Put (PE) ${metric.toUpperCase()}`;

                        if (metric === "depth_qty") {
                            keyCe = "ce_ask_qty"; keyPe = "pe_bid_qty";
                            titleCe = "Call Ask Qty (Sell Vol)"; titlePe = "Put Bid Qty (Buy Vol)";
                        }

                        const ceVals = snapshots.map(s => s[keyCe] || 0);
                        const peVals = snapshots.map(s => s[keyPe] || 0);

                        datasets.push({
                            label: titleCe,
                            data: ceVals,
                            borderColor: '#ef4444',
                            backgroundColor: 'rgba(239, 68, 68, 0.1)',
                            borderWidth: 2,
                            tension: 0.2
                        });
                        datasets.push({
                            label: titlePe,
                            data: peVals,
                            borderColor: '#22c55e',
                            backgroundColor: 'rgba(34, 197, 94, 0.1)',
                            borderWidth: 2,
                            tension: 0.2
                        });

                        datasets.push({
                            label: `${titleCe} Trend`,
                            data: computeLinearTrend(ceVals),
                            borderColor: '#f87171',
                            borderDash: [6, 4],
                            borderWidth: 2,
                            pointRadius: 0,
                            fill: false
                        });
                        datasets.push({
                            label: `${titlePe} Trend`,
                            data: computeLinearTrend(peVals),
                            borderColor: '#4ade80',
                            borderDash: [6, 4],
                            borderWidth: 2,
                            pointRadius: 0,
                            fill: false
                        });
                    }

                    historyChartObj.data.labels = times;
                    historyChartObj.data.datasets = datasets;
                    historyChartObj.update();
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
                        { type: 'bar', label: 'Calls (CE)', data: [], backgroundColor: 'rgba(239, 68, 68, 0.85)', borderColor: '#ef4444', borderWidth: 1 },
                        { type: 'bar', label: 'Puts (PE)', data: [], backgroundColor: 'rgba(34, 197, 94, 0.85)', borderColor: '#22c55e', borderWidth: 1 },
                        { type: 'line', label: 'CE Trend', data: [], borderColor: '#f87171', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, fill: false },
                        { type: 'line', label: 'PE Trend', data: [], borderColor: '#4ade80', borderDash: [6, 4], borderWidth: 2, pointRadius: 0, fill: false }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
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

            let ceData = [], peData = [];
            let ceLabel = "", peLabel = "";
            let ceColor = "", peColor = "";
            let metricTitle = "";

            if (selected === "oi_chg") {
                metricTitle = "Change in Open Interest (Chg in OI)";
                ceLabel = "Call Chg in OI"; peLabel = "Put Chg in OI";
                ceColor = 'rgba(225, 29, 72, 0.85)'; peColor = 'rgba(16, 185, 129, 0.85)';
                ceData = rawRows.map(r => r.ce_oi_chg);
                peData = rawRows.map(r => r.pe_oi_chg);
            }
            else if (selected === "oi") {
                metricTitle = "Open Interest (CE vs PE)";
                ceLabel = "Call OI"; peLabel = "Put OI";
                ceColor = 'rgba(239, 68, 68, 0.85)'; peColor = 'rgba(34, 197, 94, 0.85)';
                ceData = rawRows.map(r => r.ce_oi);
                peData = rawRows.map(r => r.pe_oi);
            }
            else if (selected === "vol") {
                metricTitle = "Trading Volume";
                ceLabel = "Call Volume"; peLabel = "Put Volume";
                ceColor = 'rgba(249, 115, 22, 0.85)'; peColor = 'rgba(14, 165, 233, 0.85)';
                ceData = rawRows.map(r => r.ce_vol);
                peData = rawRows.map(r => r.pe_vol);
            }
            else if (selected === "depth_qty") {
                metricTitle = "Order Book Depth Quantity (Call Ask Qty vs Put Bid Qty)";
                ceLabel = "Call Ask Qty (Sell Vol)"; peLabel = "Put Bid Qty (Buy Vol)";
                ceColor = 'rgba(236, 72, 153, 0.85)'; peColor = 'rgba(168, 85, 247, 0.85)';
                ceData = rawRows.map(r => r.ce_ask_qty);
                peData = rawRows.map(r => r.pe_bid_qty);
            }
            else if (selected === "delta") {
                metricTitle = "Option Delta Exposure";
                ceLabel = "Call Delta"; peLabel = "Put Delta";
                ceColor = 'rgba(239, 68, 68, 0.85)'; peColor = 'rgba(34, 197, 94, 0.85)';
                ceData = rawRows.map(r => r.ce_delta);
                peData = rawRows.map(r => r.pe_delta);
            }
            else if (selected === "theta") {
                metricTitle = "Theta Time Decay Rate";
                ceLabel = "Call Theta"; peLabel = "Put Theta";
                ceColor = 'rgba(234, 179, 8, 0.85)'; peColor = 'rgba(168, 85, 247, 0.85)';
                ceData = rawRows.map(r => r.ce_theta);
                peData = rawRows.map(r => r.pe_theta);
            }

            titleEl.innerText = metricTitle;

            myChart.data.labels = strikes;
            
            myChart.data.datasets[0].label = ceLabel;
            myChart.data.datasets[0].data = ceData;
            myChart.data.datasets[0].backgroundColor = ceColor;

            myChart.data.datasets[1].label = peLabel;
            myChart.data.datasets[1].data = peData;
            myChart.data.datasets[1].backgroundColor = peColor;

            myChart.data.datasets[2].label = `${ceLabel} Trend`;
            myChart.data.datasets[2].data = computeLinearTrend(ceData);

            myChart.data.datasets[3].label = `${peLabel} Trend`;
            myChart.data.datasets[3].data = computeLinearTrend(peData);

            myChart.update();

            const count = rawRows.length || 1;
            const ceSum = ceData.reduce((a, b) => a + Math.abs(b), 0);
            const peSum = peData.reduce((a, b) => a + Math.abs(b), 0);
            const ceAvg = (ceSum / count).toFixed(2);
            const peAvg = (peSum / count).toFixed(2);
            
            const totalSum = ceSum + peSum;
            const cePct = totalSum > 0 ? ((ceSum / totalSum) * 100).toFixed(1) : 50;
            const pePct = totalSum > 0 ? ((peSum / totalSum) * 100).toFixed(1) : 50;
            const ratio = ceSum > 0 ? (peSum / ceSum).toFixed(2) : "0.00";

            document.getElementById("gaugeMetricName").innerText = metricTitle.toUpperCase();
            document.getElementById("gaugeCeLabel").innerText = `CALL (CE) TOTAL: ${ceSum.toLocaleString()} (${cePct}%)`;
            document.getElementById("gaugePeLabel").innerText = `PUT (PE) TOTAL: ${peSum.toLocaleString()} (${pePct}%)`;
            document.getElementById("gaugeCeAvg").innerText = `Call Avg: ${Number(ceAvg).toLocaleString()}`;
            document.getElementById("gaugePeAvg").innerText = `Put Avg: ${Number(peAvg).toLocaleString()}`;
            document.getElementById("gaugeRatioText").innerText = `Put/Call Ratio: ${ratio}`;

            document.getElementById("gaugeCeBar").style.width = `${cePct}%`;
            document.getElementById("gaugePeBar").style.width = `${pePct}%`;
        }

        let timerSeconds = 10;
        function startAutoSync() {
            setInterval(() => {
                timerSeconds--;
                if (timerSeconds <= 0) {
                    timerSeconds = 10;
                    fetchDataInBackground();
                }
                document.getElementById("countdown").innerText = timerSeconds;
            }, 1000);
        }

        function fetchDataInBackground() {
            fetch(`/api/data?symbol=${currentSymbol}&expiry=${currentExpiry}`)
                .then(res => res.json())
                .then(data => {
                    renderPageData(data);
                    loadHistoricalChart();
                    loadCandleChart();
                })
                .catch(err => {
                    console.error("Auto Sync Error:", err);
                });
        }

        try { initHistoryChart(); } catch(e) { console.error("History Chart Init Error:", e); }
        try { initChart(); } catch(e) { console.error("Bar Chart Init Error:", e); }
        renderPageData({
            rows: rawRows,
            spot_price: {{ spot_price }},
            atm_strike: {{ atm_strike }},
            expiry: "{{ expiry }}",
            updated: "{{ updated }}",
            pcr: {{ pcr }}
        });
        try { initCandleChart(); } catch(e) { console.error("Candle Chart Init Error:", e); }

        startAutoSync();
    </script>
</body>
</html>
"""

# --- ROUTES ---
@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/history")
def api_history():
    """Returns stored in-memory 5-minute snapshots filtered by symbol & resampled by timeframe."""
    symbol = request.args.get("symbol", "NIFTY").upper()
    timeframe = request.args.get("timeframe", "5minute")

    with SNAPSHOT_LOCK:
        filtered = [s for s in HISTORICAL_SNAPSHOTS if s.get("symbol") == symbol]

    if timeframe == "5minute" or not filtered:
        return jsonify({"history": filtered, "count": len(filtered)})

    sec_map = {
        "15minute": 15 * 60,
        "30minute": 30 * 60,
        "60minute": 60 * 60,
        "day": 24 * 60 * 60
    }
    interval_sec = sec_map.get(timeframe, 300)

    grouped = {}
    for item in filtered:
        bucket = (item["timestamp"] // interval_sec) * interval_sec
        grouped[bucket] = item

    resampled = [grouped[b] for b in sorted(grouped.keys())]
    return jsonify({"history": resampled, "count": len(resampled)})

@app.route("/api/candles")
def api_candles():
    """Returns historical & today's intraday live candle OHLC data with 8 EMA and 24 EMA."""
    symbol = request.args.get("symbol", "NIFTY").upper()
    timeframe = request.args.get("timeframe", "5minute")
    data, error = fetch_historical_candles(symbol, timeframe, days=60)
    
    if error:
        return jsonify({"candles": [], "ema8": [], "ema24": [], "error": error})
    return jsonify({
        "candles": data["candles"],
        "ema8": data["ema8"],
        "ema24": data["ema24"],
        "error": None
    })

@app.route("/api/data")
def api_data():
    """Background JSON API endpoint for zero-reload live updates."""
    symbol = request.args.get("symbol", "NIFTY").upper()
    selected_expiry = request.args.get("expiry")
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
    selected_expiry = request.args.get("expiry")
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
    selected_expiry = request.args.get("expiry")
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
    selected_expiry = request.args.get("expiry")
    rows, spot_price, atm_strike, expiry, expiries, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol, selected_expiry)

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([f"Symbol: {symbol}", f"Spot Price: {spot_price}", f"ATM Strike: {atm_strike}", f"Expiry: {expiry}", f"PCR: {pcr}", f"Updated: {updated}"])
    cw.writerow([
        "CE OI", "CE Vol", "CE IV", "CE Delta", "CE Theta", "CE Vega", "CE Gamma", "CE Bid", "CE Ask", "CE LTP",
        "STRIKE",
        "PE LTP", "PE Bid", "PE Ask", "PE Delta", "PE Theta", "PE Vega", "PE Gamma", "PE IV", "PE Vol", "PE OI"
    ])

    if not error:
        for r in rows:
            cw.writerow([
                r["ce_oi"], r["ce_vol"], r["ce_iv"], r["ce_delta"], r["ce_theta"], r["ce_vega"], r["ce_gamma"], r["ce_bid"], r["ce_ask"], r["ce_ltp"],
                r["strike"],
                r["pe_ltp"], r["pe_bid"], r["pe_ask"], r["pe_delta"], r["pe_theta"], r["pe_vega"], r["pe_gamma"], r["pe_iv"], r["pe_vol"], r["pe_oi"]
            ])

    output = Response(si.getvalue(), mimetype="text/csv")
    output.headers["Content-Disposition"] = f"attachment; filename={symbol}_{expiry}_Option_Chain.csv"
    return output

if __name__ == "__main__":
    start_background_recorder()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
