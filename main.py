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

        # Consolidated Expiry Handling
        if selected_expiry == "ALL":
            combined_chain = []
            # Fetch up to 6 active expiries in parallel
            target_expiries = expiries[:6]
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(fetch_single_expiry_chain, instrument_key, exp, headers) for exp in target_expiries]
                for future in as_completed(futures):
                    _, data = future.result()
                    if data:
                        combined_chain.extend(data)
            
            return combined_chain, "ALL EXPIRIES (CONSOLIDATED)", expiries, None

        # Single Expiry Handling
        target_expiry = selected_expiry if (selected_expiry and selected_expiry in expiries) else expiries[0]
        _, chain_data = fetch_single_expiry_chain(instrument_key, target_expiry, headers)
        return chain_data, target_expiry, expiries, None

    except Exception as e:
        return None, None, [], f"Exception during fetch: {e}"

def process_option_data(symbol, selected_expiry=None):
    chain_data, current_expiry, expiries, error = fetch_option_chain(symbol, selected_expiry)
    
    # IST Timezone Formatting
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    updated_time = datetime.now(ist_tz).strftime("%d/%m/%Y %H:%M:%S IST")

    if error or not chain_data:
        return [], 0, 0, current_expiry or "N/A", expiries, updated_time, 0, 0, 0, error or "No option chain data available."

    spot_price = chain_data[0].get("underlying_spot_price", 0) if chain_data else 0

    # Grid Stabilization: Calculate fixed ATM strike and symmetric strike range
    step = 100 if symbol == "BANKNIFTY" else 50
    atm_strike = round(spot_price / step) * step if step > 0 else spot_price

    num_strikes_each_side = 12
    min_strike = atm_strike - (num_strikes_each_side * step)
    max_strike = atm_strike + (num_strikes_each_side * step)

    # Consolidated Aggregation Map across Expiries
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

        # Accumulate / Sum Volume and Open Interest Metrics
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

        # Preserve LTP & Greeks from the primary / nearest expiry
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
                    <td class
