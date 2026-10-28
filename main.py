import os
import time
import io
import csv
import urllib.parse
from urllib.parse import parse_qs, urlparse
import requests
import pyotp
from flask import Flask, render_template_string, request, Response

# --- FLASK APP INITIALIZATION ---
app = Flask(__name__)

# --- ENVIRONMENT CONFIGURATION FROM RENDER ---
CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID")
CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET")
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1:5000/")
USER_ID = os.getenv("UPSTOX_USER_ID")
PIN = os.getenv("UPSTOX_PIN")
TOTP_KEY = os.getenv("UPSTOX_TOTP_KEY")

INSTRUMENT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank"
}

# In-memory token cache
TOKEN_CACHE = {
    "access_token": None,
    "generated_at": 0
}

# --- AUTOMATED TOKEN GENERATION FLOW ---
def generate_upstox_token():
    """
    Automates Upstox login using the dedicated TOTP API endpoint.
    """
    if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, USER_ID, PIN, TOTP_KEY]):
        print("[Auth Error] Missing one or more required Upstox Environment Variables!")
        return None

    try:
        print("[Auth] Generating TOTP code...")
        totp_code = pyotp.TOTP(TOTP_KEY).now()

        session = requests.Session()

        # Step 1: Submit TOTP & PIN directly to Upstox TOTP API
        totp_url = "https://api.upstox.com/v2/totp-login/authorization/token"
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": CLIENT_ID
        }
        payload = {
            "totp": totp_code,
            "pin": PIN
        }

        print("[Auth Step 1] Submitting TOTP and PIN...")
        res_totp = session.post(totp_url, json=payload, headers=headers, timeout=10)
        print(f"[Auth Step 1] Status Code: {res_totp.status_code}")

        if res_totp.status_code != 200:
            print(f"[Auth Step 1 Error] TOTP verification failed: {res_totp.text}")
            return None

        # Step 2: Request Authorization Code
        encoded_redirect = urllib.parse.quote(REDIRECT_URI, safe='')
        dialog_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={CLIENT_ID}&redirect_uri={encoded_redirect}"

        print("[Auth Step 2] Requesting Authorization Dialog...")
        res_dialog = session.get(dialog_url, allow_redirects=False, timeout=10)
        
        location_header = res_dialog.headers.get("Location", "")
        if not location_header:
            print(f"[Auth Step 2 Error] No Location header returned ({res_dialog.status_code}): {res_dialog.text}")
            return None

        parsed_url = urlparse(location_header)
        auth_code = parse_qs(parsed_url.query).get("code", [None])[0]

        if not auth_code:
            print(f"[Auth Step 2 Error] Failed to extract auth code from Location header: {location_header}")
            return None

        # Step 3: Exchange Auth Code for Access Token
        token_url = "https://api.upstox.com/v2/login/authorization/token"
        token_headers = {
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        token_payload = {
            "code": auth_code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code"
        }

        print("[Auth Step 3] Exchanging Auth Code for Access Token...")
        res_token = session.post(token_url, data=token_payload, headers=token_headers, timeout=10)

        if res_token.status_code == 200:
            token = res_token.json().get("access_token")
            TOKEN_CACHE["access_token"] = token
            TOKEN_CACHE["generated_at"] = time.time()
            print("[Auth Success] Fresh Upstox Access Token generated successfully!")
            return token
        else:
            print(f"[Auth Step 3 Error] Token Exchange failed ({res_token.status_code}): {res_token.text}")

    except Exception as e:
        print(f"[Auth Exception] Exception during automated login: {e}")

    return None

def get_valid_token():
    if TOKEN_CACHE["access_token"] and (time.time() - TOKEN_CACHE["generated_at"] < 43200):
        return TOKEN_CACHE["access_token"]
    return generate_upstox_token()

# --- UPSTOX DATA FETCHING ---
def fetch_option_chain(symbol):
    token = get_valid_token()
    if not token:
        return None, None, "Failed to generate Upstox access token. Check Render logs for Auth errors."

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    try:
        expiry_url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}"
        exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code == 401:
            print("[Auth] Token invalid/expired. Forcing regeneration...")
            token = generate_upstox_token()
            if not token:
                return None, None, "Token expired and regeneration failed."
            headers["Authorization"] = f"Bearer {token}"
            exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code != 200:
            return None, None, f"Expiry lookup failed: {exp_res.text}"

        expiries = sorted(list(set(item["expiry"] for item in exp_res.json().get("data", []))))
        if not expiries:
            return None, None, "No option contract expiries found."

        current_expiry = expiries[0]

        chain_url = f"https://api.upstox.com/v2/option/chain?instrument_key={urllib.parse.quote(instrument_key)}&expiry_date={current_expiry}"
        chain_res = requests.get(chain_url, headers=headers, timeout=10)

        if chain_res.status_code != 200:
            return None, current_expiry, f"Option Chain fetch failed: {chain_res.text}"

        chain_data = chain_res.json().get("data", [])
        return chain_data, current_expiry, None

    except Exception as e:
        return None, None, f"Exception during fetch: {e}"

def process_option_data(symbol):
    chain_data, current_expiry, error = fetch_option_chain(symbol)
    updated_time = time.strftime("%m/%d/%Y %H:%M:%S")

    if error or not chain_data:
        return [], 0, current_expiry or "N/A", updated_time, error or "No option chain data available."

    spot_price = chain_data[0].get("underlying_spot_price", 0) if chain_data else 0
    range_limit = 1200 if symbol == "BANKNIFTY" else 600
    rows = []

    for item in chain_data:
        strike = item.get("strike_price", 0)
        if abs(strike - spot_price) <= range_limit:
            ce = item.get("call_options", {}) or {}
            pe = item.get("put_options", {}) or {}

            ce_mkt = ce.get("market_data", {}) or {}
            ce_greek = ce.get("option_greeks", {}) or {}
            pe_mkt = pe.get("market_data", {}) or {}
            pe_greek = pe.get("option_greeks", {}) or {}

            rows.append({
                "ce_oi": ce_mkt.get("oi", 0),
                "ce_vol": ce_mkt.get("volume", 0),
                "ce_iv": round(ce_greek.get("iv", 0), 2) if ce_greek else 0,
                "ce_ltp": ce_mkt.get("ltp", 0),
                "strike": strike,
                "pe_ltp": pe_mkt.get("ltp", 0),
                "pe_iv": round(pe_greek.get("iv", 0), 2) if pe_greek else 0,
                "pe_vol": pe_mkt.get("volume", 0),
                "pe_oi": pe_mkt.get("oi", 0)
            })

    return rows, spot_price, current_expiry, updated_time, None

# --- HTML TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Upstox Option Chain Dashboard</title>
    <meta http-equiv="refresh" content="60">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #f4f6f9; }
        h2 { color: #222; margin-bottom: 10px; }
        .info-box { background: #fff; padding: 16px; border-radius: 6px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.08); }
        .btn { display: inline-block; padding: 9px 18px; color: #fff; background-color: #007bff; text-decoration: none; border-radius: 4px; margin-right: 10px; font-weight: 500; }
        .btn-success { background-color: #28a745; }
        .btn:hover { opacity: 0.9; }
        table { width: 100%; border-collapse: collapse; background: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        th, td { border: 1px solid #e0e0e0; padding: 9px; text-align: center; font-size: 14px; }
        th { background-color: #007bff; color: white; }
        tr:nth-child(even){ background-color: #f8f9fa; }
        .atm { background-color: #fff3cd !important; font-weight: bold; color: #856404; }
        .ce-header { background-color: #28a745 !important; }
        .pe-header { background-color: #dc3545 !important; }
        .error-msg { color: #dc3545; font-weight: bold; padding: 12px; background: #f8d7da; border-radius: 4px; }
    </style>
</head>
<body>
    <h2>Upstox Live Option Chain</h2>
    <div class="info-box">
        <strong>Symbol:</strong> {{ symbol }} | 
        <strong>Spot Price:</strong> {{ spot_price }} | 
        <strong>Expiry:</strong> {{ expiry }} | 
        <strong>Updated:</strong> {{ updated }}
        <br><br>
        <a href="/?symbol=NIFTY" class="btn">View NIFTY</a>
        <a href="/?symbol=BANKNIFTY" class="btn">View BANKNIFTY</a>
        <a href="/download?symbol={{ symbol }}" class="btn btn-success">Download CSV</a>
    </div>

    {% if error %}
        <div class="error-msg">Error: {{ error }}</div>
    {% else %}
    <table>
        <thead>
            <tr>
                <th colspan="4" class="ce-header">CALLS</th>
                <th>STRIKE</th>
                <th colspan="4" class="pe-header">PUTS</th>
            </tr>
            <tr>
                <th>OI</th><th>Volume</th><th>IV</th><th>LTP</th>
                <th>Price</th>
                <th>LTP</th><th>IV</th><th>Volume</th><th>OI</th>
            </tr>
        </thead>
        <tbody>
            {% for row in rows %}
            <tr>
                <td>{{ row.ce_oi }}</td>
                <td>{{ row.ce_vol }}</td>
                <td>{{ row.ce_iv }}</td>
                <td>{{ row.ce_ltp }}</td>
                <td class="atm">{{ row.strike }}</td>
                <td>{{ row.pe_ltp }}</td>
                <td>{{ row.pe_iv }}</td>
                <td>{{ row.pe_vol }}</td>
                <td>{{ row.pe_oi }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% endif %}
</body>
</html>
"""

# --- ROUTES ---
@app.route("/")
def index():
    symbol = request.args.get("symbol", "NIFTY").upper()
    rows, spot_price, expiry, updated, error = process_option_data(symbol)
    return render_template_string(
        HTML_TEMPLATE,
        rows=rows,
        symbol=symbol,
        spot_price=spot_price,
        expiry=expiry,
        updated=updated,
        error=error
    )

@app.route("/download")
def download_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    rows, spot_price, expiry, updated, error = process_option_data(symbol)

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([f"Symbol: {symbol}", f"Spot Price: {spot_price}", f"Expiry: {expiry}", f"Updated: {updated}"])
    cw.writerow(["CALLS OI", "CALLS Vol", "CALLS IV", "CALLS LTP", "STRIKE", "PUTS LTP", "PUTS IV", "PUTS Vol", "PUTS OI"])

    if not error:
        for r in rows:
            cw.writerow([r["ce_oi"], r["ce_vol"], r["ce_iv"], r["ce_ltp"], r["strike"], r["pe_ltp"], r["pe_iv"], r["pe_vol"], r["pe_oi"]])

    output = Response(si.getvalue(), mimetype="text/csv")
    output.headers["Content-Disposition"] = f"attachment; filename={symbol}_Option_Chain.csv"
    return output

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
