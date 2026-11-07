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
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM")

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

def fetch_history_from_gsheet(symbol="NIFTY"):
    sheet_id = SPREADSHEET_ID or "1fpXdodshvwHcduwffOhEK54K-a7a4TmOZF2Q-EgVRzM"
    candidate_sheets = ["NIFTY", "NITY", symbol.upper(), "Sheet1", ""]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    raw_csv = None
    last_err = None

    for sheet_name in candidate_sheets:
        if sheet_name:
            url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
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
            if "time" in l_lower or "symbol" in l_lower or "pcr" in l_lower or "ltp" in l_lower:
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

            time_val = str(norm_r.get("time", norm_r.get("datetime", norm_r.get("timestamp", ""))))
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
                # Main Signal Columns (AE:AI)
                "min_roc": safe_num("Min ROC (Buy)", ["min_roc", "min roc"]),
                "max_roc": safe_num("Max ROC (Sell)", ["max_roc", "max roc"]),
                "cross_roc": safe_num("Cross ROC Sig", ["cross_roc_sig", "cross roc"]),
                "main_volatility": safe_num("Main Volatility", ["main_vol", "volatility"]),
                "new_signal": safe_str("New Signal", ["signal", "sig"])
            }
            snapshots.append(snapshot)

        if not snapshots:
            return [], "Google Sheet connected but no valid data rows were found matching expected columns."

        return snapshots[::-1], None

    except Exception as e:
        print(f"[GSheet Parsing Exception]: {e}")
        return [], f"Parsing error reading Google Sheet CSV: {e}"

# --- UPSTOX LIVE TRADE & POSITION FETCHING ---
def fetch_upstox_positions():
    token = get_valid_token()
    if not token:
        return [], [], 0.0, 0.0, 0.0, "Authentication paused or invalid."

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    try:
        pos_url = "https://api.upstox.com/v2/portfolio/short-term-positions"
        pos_res = requests.get(pos_url, headers=headers, timeout=10)

        if pos_res.status_code == 401:
            invalidate_session()
            token = perform_single_auth()
            if not token:
                return [], [], 0.0, 0.0, 0.0, "Session expired."
            headers["Authorization"] = f"Bearer {token}"
            pos_res = requests.get(pos_url, headers=headers, timeout=10)

        positions_raw = pos_res.json().get("data", []) if pos_res.status_code == 200 else []

        ord_url = "https://api.upstox.com/v2/order/retrieve-all"
        ord_res = requests.get(ord_url, headers=headers, timeout=10)
        orders_raw = ord_res.json().get("data", []) if ord_res.status_code == 200 else []

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

        return positions, orders, round(total_pnl, 2), round(realised_pnl, 2), round(unrealised_pnl, 2), None

    except Exception as e:
        print(f"[Positions Exception] Error fetching positions: {e}")
        return [], [], 0.0, 0.0, 0.0, str(e)

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
    
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    updated_time = datetime.now(ist_tz).strftime("%d/%m/%Y %H:%M:%S IST")

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
        .gauge-ce { background-color: #ef4444; height: 100%; transition: width 0.4s ease; }
        .gauge-pe { background-color: #22c55e; height: 100%; transition: width 0.4s ease; }
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
            <option value="depth_qty">1 - Order Book Depth Quantity (Call Ask Qty vs Put Bid Qty)</option>
            <option value="oi">Open Interest (CE vs PE)</option>
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

    <!-- CHART 1: STRIKE BAR CHART -->
    <div class="chart-box">
        <div class="chart-header">
            <div class="chart-title" id="chartHeaderTitle">Change in Open Interest (Chg in OI)</div>
            <span style="color: #facc15; font-size: 13px; font-weight: bold;">★ Yellow Line = Spot ATM Strike | Dashed Lines = Linear Trendlines</span>
        </div>
        <canvas id="analysisChart" style="max-height: 480px;"></canvas>
    </div>

    <!-- CHART 2: HISTORICAL SNAPSHOT TREND (GOOGLE SHEET MEMORY) -->
    <div class="chart-box">
        <div class="chart-header">
            <div style="display: flex; align-items: center; gap: 15px;">
                <span class="chart-title">2 - 📈 Historical Snapshot Trend (Google Sheet Memory)</span>
            </div>
            <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
                <label style="color: #94a3b8; font-weight: bold;">Head of Dropdown:</label>
                <select id="historyMetricSelector" onchange="loadHistoricalChart()">
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
                <select id="historyDayRangeSelector" onchange="loadHistoricalChart()">
                    <option value="1week" selected>1 Week</option>
                    <option value="2week">2 Week</option>
                    <option value="1month">1 Month</option>
                    <option value="3month">3 Month</option>
                </select>

                <label style="color: #94a3b8; font-weight: bold; margin-left: 6px;">Interval:</label>
                <select id="historyTimeframeSelector" onchange="loadHistoricalChart()">
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

    <!-- CHART 3: MAIN SIGNAL CHART (COLUMNS AE TO AI) -->
    <div class="chart-box">
        <div class="chart-header">
            <div style="display: flex; align-items: center; gap: 15px;">
                <span class="chart-title">3 - 🚀 Main Signal</span>
            </div>
            <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
                <label style="color: #94a3b8; font-weight: bold;">Data Day Range:</label>
                <select id="signalDayRangeSelector" onchange="loadSignalChart()">
                    <option value="1week" selected>1 Week</option>
                    <option value="2week">2 Week</option>
                    <option value="1month">1 Month</option>
                    <option value="3month">3 Month</option>
                </select>

                <label style="color: #94a3b8; font-weight: bold; margin-left: 6px;">Interval:</label>
                <select id="signalTimeframeSelector" onchange="loadSignalChart()">
                    <option value="5minute" selected>5 Min</option>
                    <option value="15minute">15 Min</option>
                    <option value="30minute">30 Min</option>
                    <option value="60minute">1 Hour</option>
                    <option value="day">1 Day</option>
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

        // Custom Plugin 1: Draw White 'B' / 'S' Signal Labels above Main Volatility line points
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
                                ctx.fillStyle = '#ffffff'; // White label text
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

        // Custom Plugin 2: Background shading for >0 (Light Green) & <0 (Light Red) and Bold Yellow 0 Line
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
                    // Light green background above 0 line (> 0)
                    ctx.fillStyle = 'rgba(34, 197, 94, 0.08)';
                    ctx.fillRect(left, top, right - left, zeroY - top);

                    // Light red background below 0 line (< 0)
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.08)';
                    ctx.fillRect(left, zeroY, right - left, bottom - zeroY);

                    // Bold yellow 0 line
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
            loadSignalChart();
        }

        function loadSignalChart() {
            const dayRange = document.getElementById('signalDayRangeSelector').value;
            const timeframe = document.getElementById('signalTimeframeSelector').value;

            fetch(`/api/history?symbol=${currentSymbol}&timeframe=${timeframe}&day_range=${dayRange}`)
                .then(res => res.json())
                .then(data => {
                    if (data.error) {
                        signalChartObj.data.labels = [data.error];
                        signalChartObj.data.datasets = [{ label: 'Status / Error', data: [0], borderColor: '#ef4444' }];
                        signalChartObj.update();
                        return;
                    }

                    const snapshots = data.history || [];
                    const times = snapshots.map(s => s.formatted_time || s.datetime);

                    if (snapshots.length === 0) {
                        signalChartObj.data.labels = ["No data found for Main Signal."];
                        signalChartObj.data.datasets = [{ label: 'Status', data: [0], borderColor: '#94a3b8' }];
                        signalChartObj.update();
                        return;
                    }

                    const minRocVals = snapshots.map(s => s.min_roc || 0);
                    const maxRocVals = snapshots.map(s => s.max_roc || 0);
                    const crossRocVals = snapshots.map(s => s.cross_roc || 0);
                    const mainVolVals = snapshots.map(s => s.main_volatility || 0);
                    const newSignals = snapshots.map(s => s.new_signal || "");

                    signalChartObj.data.labels = times;
                    signalChartObj.data.datasets = [
                        {
                            label: 'Min ROC (Buy)',
                            data: minRocVals,
                            borderColor: '#4ade80', // Light Green
                            backgroundColor: 'rgba(74, 222, 128, 0.1)',
                            borderWidth: 2,
                            tension: 0.2
                        },
                        {
                            label: 'Max ROC (Sell)',
                            data: maxRocVals,
                            borderColor: '#ef4444', // Red
                            backgroundColor: 'rgba(239, 68, 68, 0.1)',
                            borderWidth: 2,
                            tension: 0.2
                        },
                        {
                            label: 'Cross ROC Sig',
                            data: crossRocVals,
                            borderColor: '#facc15', // Yellow
                            backgroundColor: 'rgba(250, 204, 21, 0.1)',
                            borderWidth: 2,
                            tension: 0.2
                        },
                        {
                            label: 'Main Volatility',
                            data: mainVolVals,
                            signalLabels: newSignals,
                            borderColor: '#38bdf8', // Sky Blue
                            backgroundColor: 'rgba(56, 189, 248, 0.1)',
                            borderWidth: 3,
                            pointRadius: 4,
                            pointHoverRadius: 6,
                            tension: 0.2
                        }
                    ];

                    signalChartObj.update();
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
            loadHistoricalChart();
        }

        function loadHistoricalChart() {
            const metric = document.getElementById('historyMetricSelector').value;
            const dayRange = document.getElementById('historyDayRangeSelector').value;
            const timeframe = document.getElementById('historyTimeframeSelector').value;

            fetch(`/api/history?symbol=${currentSymbol}&timeframe=${timeframe}&metric=${metric}&day_range=${dayRange}`)
                .then(res => res.json())
                .then(data => {
                    if (data.error) {
                        historyChartObj.data.labels = [data.error];
                        historyChartObj.data.datasets = [{ label: 'Status / Error', data: [0], borderColor: '#ef4444' }];
                        historyChartObj.update();
                        return;
                    }

                    const snapshots = data.history || [];
                    const times = snapshots.map(s => s.formatted_time || s.datetime);

                    let datasets = [];

                    if (snapshots.length === 0) {
                        historyChartObj.data.labels = ["No data found in Google Sheet."];
                        historyChartObj.data.datasets = [{ label: 'Status', data: [0], borderColor: '#94a3b8' }];
                        historyChartObj.update();
                        return;
                    }

                    if (metric === "price_pcr") {
                        historyChartObj.options.scales.y1.display = true;
                        const spotVals = snapshots.map(s => s.spot_price);
                        const pcrVals = snapshots.map(s => s.pcr);
                        const avgVals = spotVals.map((v, i) => (v + (pcrVals[i] || 0)) / 2);

                        // PCR on LEFT axis (y)
                        datasets.push({
                            label: 'Relation 2: PCR',
                            data: pcrVals,
                            borderColor: '#06b6d4',
                            backgroundColor: 'rgba(6, 182, 212, 0.1)',
                            borderWidth: 2,
                            tension: 0.2,
                            yAxisID: 'y',
                            fill: false
                        });

                        // Price LTP on RIGHT axis (y1)
                        datasets.push({
                            label: 'Relation 1: Price LTP',
                            data: spotVals,
                            borderColor: '#facc15',
                            backgroundColor: 'rgba(250, 204, 21, 0.1)',
                            borderWidth: 2,
                            tension: 0.2,
                            yAxisID: 'y1',
                            fill: true
                        });

                        // Average Line in Sky Blue (#38bdf8)
                        datasets.push({
                            label: 'Average (Price & PCR)',
                            data: avgVals,
                            borderColor: '#38bdf8',
                            backgroundColor: 'rgba(56, 189, 248, 0.1)',
                            borderWidth: 2,
                            tension: 0.2,
                            yAxisID: 'y1',
                            fill: false
                        });

                        // Price Trend on RIGHT axis (y1)
                        datasets.push({
                            label: 'Price Trend',
                            data: computeLinearTrend(spotVals),
                            borderColor: '#ca8a04',
                            borderDash: [6, 4],
                            borderWidth: 2,
                            pointRadius: 0,
                            yAxisID: 'y1',
                            fill: false
                        });
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

                        datasets.push({
                            label: `Relation 1: ${cfg.r1}`,
                            data: ceVals,
                            borderColor: '#ef4444',
                            backgroundColor: 'rgba(239, 68, 68, 0.1)',
                            borderWidth: 2,
                            tension: 0.2,
                            yAxisID: 'y'
                        });

                        datasets.push({
                            label: `Relation 2: ${cfg.r2}`,
                            data: peVals,
                            borderColor: '#22c55e',
                            backgroundColor: 'rgba(34, 197, 94, 0.1)',
                            borderWidth: 2,
                            tension: 0.2,
                            yAxisID: 'y'
                        });

                        // Sky Blue Average Line
                        datasets.push({
                            label: `Average (${cfg.r1} & ${cfg.r2})`,
                            data: avgVals,
                            borderColor: '#38bdf8', // Sky Blue
                            backgroundColor: 'rgba(56, 189, 248, 0.1)',
                            borderWidth: 2,
                            tension: 0.2,
                            yAxisID: 'y',
                            fill: false
                        });

                        datasets.push({
                            label: `${cfg.r1} Trend`,
                            data: computeLinearTrend(ceVals),
                            borderColor: '#f87171',
                            borderDash: [6, 4],
                            borderWidth: 2,
                            pointRadius: 0,
                            yAxisID: 'y',
                            fill: false
                        });

                        datasets.push({
                            label: `${cfg.r2} Trend`,
                            data: computeLinearTrend(peVals),
                            borderColor: '#4ade80',
                            borderDash: [6, 4],
                            borderWidth: 2,
                            pointRadius: 0,
                            yAxisID: 'y',
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

            if (selected === "depth_qty") {
                metricTitle = "1 - Order Book Depth Quantity (Call Ask Qty vs Put Bid Qty)";
                ceLabel = "Call Ask Qty (Sell Vol)"; peLabel = "Put Bid Qty (Buy Vol)";
                ceColor = 'rgba(236, 72, 153, 0.85)'; peColor = 'rgba(168, 85, 247, 0.85)';
                ceData = rawRows.map(r => r.ce_ask_qty);
                peData = rawRows.map(r => r.pe_bid_qty);
            }
            else if (selected === "oi_chg") {
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
                    loadHistoricalChart();
                    loadSignalChart();
                })
                .catch(err => {
                    console.error("Auto Sync Error:", err);
                })
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
        .subtitle { color: #94a3b8; font-size: 13px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
        .timer-badge { background: #0284c7; color: #ffffff; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        
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
        <span>Real-time Upstox Portfolio & Executed Order Log — Background Auto-Sync</span>
        <span class="timer-badge" id="syncBadge">Auto Sync: <span id="countdown">10</span>s</span>
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

        function fetchPositionsInBackground() {
            isSyncing = true;
            document.getElementById("syncBadge").innerText = "Syncing...";

            fetch('/api/positions')
                .then(res => res.json())
                .then(data => {
                    const errEl = document.getElementById("errorContainer");
                    if (data.error) {
                        errEl.style.display = "block";
                        errEl.innerText = "Error Fetching Positions: " + data.error;
                    } else {
                        errEl.style.display = "none";
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

@app.route("/api/positions")
def api_positions():
    """Background API route fetching live Upstox positions and order log."""
    positions, orders, total_pnl, realised_pnl, unrealised_pnl, error = fetch_upstox_positions()
    return jsonify({
        "positions": positions,
        "orders": orders,
        "total_pnl": total_pnl,
        "realised_pnl": realised_pnl,
        "unrealised_pnl": unrealised_pnl,
        "error": error
    })

@app.route("/Live_Trade_Position", methods=["GET"])
def live_trade_position():
    """Unlisted private route for real-time portfolio, trade logs & P&L."""
    return render_template_string(POSITIONS_HTML_TEMPLATE)

@app.route("/api/history")
def api_history():
    symbol = request.args.get("symbol", "NIFTY").upper()
    timeframe = request.args.get("timeframe", "5minute")
    day_range = request.args.get("day_range", "1week").lower()

    snapshots, err = fetch_history_from_gsheet(symbol)

    if err:
        return jsonify({"history": [], "count": 0, "error": err})

    if not snapshots:
        return jsonify({"history": [], "count": 0})

    day_map = {
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
