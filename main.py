import os
import time
import io
import csv
import json
import urllib.parse
import requests
import pyotp
from flask import Flask, render_template_string, request, Response

app = Flask(__name__)

CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID")
CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET")
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1:5000/")
USER_ID = os.getenv("UPSTOX_USER_ID")
PIN = os.getenv("UPSTOX_PIN")
TOTP_KEY = os.getenv("UPSTOX_TOTP_KEY")

CACHE_FILE_PATH = "/tmp/upstox_token_cache.json"

INSTRUMENT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank"
}

AUTH_COOLDOWN = {
    "last_failure_time": 0,
    "cooldown_duration": 900
}

def load_cached_token():
    try:
        if os.path.exists(CACHE_FILE_PATH):
            with open(CACHE_FILE_PATH, "r") as f:
                data = json.load(f)
                if time.time() - data.get("generated_at", 0) < 43200:
                    return data.get("access_token")
    except Exception as e:
        print(f"[Cache Warning] Read error: {e}")
    return None

def save_token_to_cache(token):
    try:
        with open(CACHE_FILE_PATH, "w") as f:
            json.dump({
                "access_token": token,
                "generated_at": time.time()
            }, f)
    except Exception as e:
        print(f"[Cache Warning] Write error: {e}")

def run_diagnostic_auth(force_test=False):
    """
    Executes TOTP Auth and logs precise diagnostic information.
    """
    current_time = time.time()
    
    if not force_test and (current_time - AUTH_COOLDOWN["last_failure_time"] < AUTH_COOLDOWN["cooldown_duration"]):
        remaining = int(AUTH_COOLDOWN["cooldown_duration"] - (current_time - AUTH_COOLDOWN["last_failure_time"]))
        print(f"[Auth Safety] Cooldown active. Skipping request. Retry in {remaining}s or visit /test-auth.")
        return None, f"Cooldown active ({remaining}s remaining). Visit /test-auth to bypass."

    if not all([CLIENT_ID, USER_ID, PIN, TOTP_KEY]):
        msg = "[Auth Error] One or more Render environment variables are missing."
        print(msg)
        return None, msg

    try:
        clean_totp_key = str(TOTP_KEY).strip().upper().replace(" ", "").replace("-", "")
        totp_code = str(pyotp.TOTP(clean_totp_key).now()).zfill(6)
        clean_pin = str(PIN).strip()
        clean_client_id = str(CLIENT_ID).strip()

        print("=" * 60)
        print("[AUTH DIAGNOSTICS]")
        print(f"Client ID: {clean_client_id[:4]}...{clean_client_id[-4:] if len(clean_client_id)>8 else ''}")
        print(f"Calculated TOTP Code: {totp_code}")
        print(f"PIN Length: {len(clean_pin)} digits")
        print("=" * 60)

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

        res = requests.post(totp_url, json=payload, headers=headers, timeout=10)
        print(f"[Upstox Response] Status: {res.status_code} | Body: {res.text}")

        if res.status_code == 200:
            json_data = res.json()
            token = json_data.get("access_token") or json_data.get("data", {}).get("access_token")
            if token:
                save_token_to_cache(token)
                AUTH_COOLDOWN["last_failure_time"] = 0
                return token, "SUCCESS"

        AUTH_COOLDOWN["last_failure_time"] = time.time()
        return None, f"Status {res.status_code}: {res.text}"

    except Exception as e:
        AUTH_COOLDOWN["last_failure_time"] = time.time()
        return None, f"Exception: {e}"

def get_valid_token():
    cached = load_cached_token()
    if cached:
        return cached
    token, _ = run_diagnostic_auth(force_test=False)
    return token

def fetch_option_chain(symbol):
    token = get_valid_token()
    if not token:
        return None, None, "Authentication paused. Check server logs or visit /test-auth."

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    try:
        expiry_url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}"
        exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code == 401:
            if os.path.exists(CACHE_FILE_PATH):
                os.remove(CACHE_FILE_PATH)
            token, _ = run_diagnostic_auth(force_test=True)
            if not token:
                return None, None, "Token invalid and re-auth failed."
            headers["Authorization"] = f"Bearer {token}"
            exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code != 200:
            return None, None, f"Expiry lookup failed: {exp_res.text}"

        expiries = sorted(list(set(item["expiry"] for item in exp_res.json().get("data", []))))
        if not expiries:
            return None, None, "No contract expiries found."

        current_expiry = expiries[0]
        chain_url = f"https://api.upstox.com/v2/option/chain?instrument_key={urllib.parse.quote(instrument_key)}&expiry_date={current_expiry}"
        chain_res = requests.get(chain_url, headers=headers, timeout=10)

        if chain_res.status_code != 200:
            return None, current_expiry, f"Option Chain fetch failed: {chain_res.text}"

        return chain_res.json().get("data", []), current_expiry, None

    except Exception as e:
        return None, None, f"Exception: {e}"

def process_option_data(symbol):
    chain_data, current_expiry, error = fetch_option_chain(symbol)
    updated_time = time.strftime("%m/%d/%Y %H:%M:%S")

    if error or not chain_data:
        return [], 0, current_expiry or "N/A", updated_time, 0, 0, 0, error or "No data."

    spot_price = chain_data[0].get("underlying_spot_price", 0) if chain_data else 0
    range_limit = 1200 if symbol == "BANKNIFTY" else 600
    rows = []
    total_ce_oi = 0
    total_pe_oi = 0

    for item in chain_data:
        strike = item.get("strike_price", 0)
        ce = item.get("call_options", {}) or {}
        pe = item.get("put_options", {}) or {}

        ce_mkt = ce.get("market_data", {}) or {}
        ce_greek = ce.get("option_greeks", {}) or {}
        pe_mkt = pe.get("market_data", {}) or {}
        pe_greek = pe.get("option_greeks", {}) or {}

        c_oi = ce_mkt.get("oi", 0) or 0
        p_oi = pe_mkt.get("oi", 0) or 0
        total_ce_oi += c_oi
        total_pe_oi += p_oi

        if abs(strike - spot_price) <= range_limit:
            rows.append({
                "strike": strike,
                "ce_oi": c_oi,
                "ce_vol": ce_mkt.get("volume", 0) or 0,
                "ce_iv": round(ce_greek.get("iv", 0) or 0, 2),
                "ce_ltp": ce_mkt.get("ltp", 0) or 0,
                "ce_bid": ce_mkt.get("bid_price", 0) or 0,
                "ce_ask": ce_mkt.get("ask_price", 0) or 0,
                "ce_delta": round(ce_greek.get("delta", 0) or 0, 2),
                "ce_theta": round(ce_greek.get("theta", 0) or 0, 2),
                "ce_vega": round(ce_greek.get("vega", 0) or 0, 2),
                "ce_gamma": round(ce_greek.get("gamma", 0) or 0, 4),
                "pe_ltp": pe_mkt.get("ltp", 0) or 0,
                "pe_bid": pe_mkt.get("bid_price", 0) or 0,
                "pe_ask": pe_mkt.get("ask_price", 0) or 0,
                "pe_iv": round(pe_greek.get("iv", 0) or 0, 2),
                "pe_vol": pe_mkt.get("volume", 0) or 0,
                "pe_oi": p_oi,
                "pe_delta": round(pe_greek.get("delta", 0) or 0, 2),
                "pe_theta": round(pe_greek.get("theta", 0) or 0, 2),
                "pe_vega": round(pe_greek.get("vega", 0) or 0, 2),
                "pe_gamma": round(pe_greek.get("gamma", 0) or 0, 4),
            })

    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0
    return rows, spot_price, current_expiry, updated_time, pcr, total_ce_oi, total_pe_oi, None

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Upstox Pro Option Chain</title>
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
        .table-container { overflow-x: auto; background: #fff; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.06); }
        table { width: 100%; border-collapse: collapse; min-width: 1200px; }
        th, td { border: 1px solid #e9ecef; padding: 7px 9px; text-align: center; font-size: 13px; }
        th { background-color: #343a40; color: white; position: sticky; top: 0; }
        tr:nth-child(even){ background-color: #f8f9fa; }
        .atm { background-color: #fff3cd !important; font-weight: bold; color: #856404; font-size: 14px; }
        .ce-header { background-color: #28a745 !important; }
        .pe-header { background-color: #dc3545 !important; }
        .greek-col { background-color: #eef2f7; font-size: 12px; }
        .order-col { background-color: #fdfffe; font-size: 12px; color: #495057; }
        .error-msg { color: #dc3545; font-weight: bold; padding: 12px; background: #f8d7da; border-radius: 4px; }
    </style>
</head>
<body>
    <h2>Upstox Pro Option Chain & Greeks Dashboard</h2>
    <div class="info-box">
        <div class="metrics-grid">
            <div class="metric-card"><strong>Symbol:</strong> {{ symbol }}</div>
            <div class="metric-card"><strong>Spot Price:</strong> {{ spot_price }}</div>
            <div class="metric-card"><strong>Expiry:</strong> {{ expiry }}</div>
            <div class="metric-card {% if pcr >= 1.0 %}pcr-bullish{% else %}pcr-bearish{% endif %}">
                <strong>PCR:</strong> {{ pcr }} {% if pcr >= 1.0 %}(Bullish){% else %}(Bearish){% endif %}
            </div>
            <div class="metric-card"><strong>Total Call OI:</strong> {{ "{:,}".format(total_ce_oi) }}</div>
            <div class="metric-card"><strong>Total Put OI:</strong> {{ "{:,}".format(total_pe_oi) }}</div>
            <div class="metric-card"><strong>Updated:</strong> {{ updated }}</div>
        </div>
        <div>
            <a href="/?symbol=NIFTY" class="btn">View NIFTY</a>
            <a href="/?symbol=BANKNIFTY" class="btn">View BANKNIFTY</a>
            <a href="/download?symbol={{ symbol }}" class="btn btn-success">Download Full CSV</a>
            <a href="/test-auth" class="btn" style="background-color:#6c757d;">Test Auth Direct</a>
        </div>
    </div>

    {% if error %}
        <div class="error-msg">Error: {{ error }}</div>
    {% else %}
    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th colspan="10" class="ce-header">CALL OPTIONS</th>
                    <th>STRIKE</th>
                    <th colspan="10" class="pe-header">PUT OPTIONS</th>
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
                    <td>{{ "{:,}".format(row.ce_oi) }}</td>
                    <td>{{ "{:,}".format(row.ce_vol) }}</td>
                    <td>{{ row.ce_iv }}</td>
                    <td class="greek-col">{{ row.ce_delta }}</td>
                    <td class="greek-col">{{ row.ce_theta }}</td>
                    <td class="greek-col">{{ row.ce_vega }}</td>
                    <td class="greek-col">{{ row.ce_gamma }}</td>
                    <td class="order-col">{{ row.ce_bid }}</td>
                    <td class="order-col">{{ row.ce_ask }}</td>
                    <td><strong>{{ row.ce_ltp }}</strong></td>
                    <td class="atm">{{ row.strike }}</td>
                    <td><strong>{{ row.pe_ltp }}</strong></td>
                    <td class="order-col">{{ row.pe_bid }}</td>
                    <td class="order-col">{{ row.pe_ask }}</td>
                    <td class="greek-col">{{ row.pe_delta }}</td>
                    <td class="greek-col">{{ row.pe_theta }}</td>
                    <td class="greek-col">{{ row.pe_vega }}</td>
                    <td class="greek-col">{{ row.pe_gamma }}</td>
                    <td>{{ row.pe_iv }}</td>
                    <td>{{ "{:,}".format(row.pe_vol) }}</td>
                    <td>{{ "{:,}".format(row.pe_oi) }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% endif %}
</body>
</html>
"""

@app.route("/health")
def health():
    return "OK", 200

@app.route("/test-auth")
def test_auth():
    token, status_msg = run_diagnostic_auth(force_test=True)
    if token:
        return f"<h3>AUTH SUCCESS!</h3><p>Access Token: {token[:15]}...</p><a href='/'>Go to Dashboard</a>"
    return f"<h3>AUTH FAILED</h3><p>{status_msg}</p><p>Check Render logs for calculated TOTP code vs Google Authenticator.</p>"

@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return Response(status=200)

    symbol = request.args.get("symbol", "NIFTY").upper()
    rows, spot_price, expiry, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol)
    return render_template_string(
        HTML_TEMPLATE,
        rows=rows,
        symbol=symbol,
        spot_price=spot_price,
        expiry=expiry,
        updated=updated,
        pcr=pcr,
        total_ce_oi=total_ce_oi,
        total_pe_oi=total_pe_oi,
        error=error
    )

@app.route("/download")
def download_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    rows, spot_price, expiry, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol)

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([f"Symbol: {symbol}", f"Spot Price: {spot_price}", f"Expiry: {expiry}", f"PCR: {pcr}", f"Updated: {updated}"])
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
    output.headers["Content-Disposition"] = f"attachment; filename={symbol}_Advanced_Option_Chain.csv"
    return output

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
