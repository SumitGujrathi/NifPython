import os
import time
import io
import csv
import urllib.parse
import requests
from flask import Flask, render_template_string, request, Response, redirect

app = Flask(__name__)

# --- ENVIRONMENT CONFIGURATION FROM RENDER ---
CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID")
CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET")
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "https://nifsumit.onrender.com/callback")

INSTRUMENT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank"
}

# In-memory token storage
TOKEN_CACHE = {
    "access_token": None,
    "generated_at": 0
}

# --- OAUTH AUTHENTICATION FLOW ---
def get_login_url():
    """Generates the official Upstox OAuth authorization URL."""
    encoded_redirect = urllib.parse.quote(REDIRECT_URI, safe='')
    return f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={CLIENT_ID}&redirect_uri={encoded_redirect}"

@app.route("/login")
def login():
    """Redirects user to official Upstox OAuth login screen."""
    return redirect(get_login_url())

@app.route("/callback")
def callback():
    """Handles callback from Upstox, exchanging code for Access Token."""
    auth_code = request.args.get("code")
    if not auth_code:
        return "Authorization failed: No code provided in callback.", 400

    token_url = "https://api.upstox.com/v2/login/authorization/token"
    headers = {
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    payload = {
        "code": auth_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }

    try:
        response = requests.post(token_url, headers=headers, data=payload, timeout=10)
        if response.status_code == 200:
            token = response.json().get("access_token")
            TOKEN_CACHE["access_token"] = token
            TOKEN_CACHE["generated_at"] = time.time()
            print("[Auth Success] Access token successfully cached!")
            return redirect("/")
        else:
            return f"Token exchange failed ({response.status_code}): {response.text}", 400
    except Exception as e:
        return f"Callback Exception: {e}", 500

# --- DATA FETCHING ---
def fetch_option_chain(symbol):
    token = TOKEN_CACHE.get("access_token")
    if not token:
        return None, None, "Token missing. Click 'Login with Upstox' to authenticate."

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }

    try:
        expiry_url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}"
        exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code == 401:
            TOKEN_CACHE["access_token"] = None
            return None, None, "Session expired. Click 'Login with Upstox' to re-authenticate."

        if exp_res.status_code != 200:
            return None, None, f"Expiry lookup failed ({exp_res.status_code}): {exp_res.text}"

        expiries = sorted(list(set(item["expiry"] for item in exp_res.json().get("data", []))))
        if not expiries:
            return None, None, "No active option contract expiries found."

        current_expiry = expiries[0]

        chain_url = f"https://api.upstox.com/v2/option/chain?instrument_key={urllib.parse.quote(instrument_key)}&expiry_date={current_expiry}"
        chain_res = requests.get(chain_url, headers=headers, timeout=10)

        if chain_res.status_code != 200:
            return None, current_expiry, f"Option Chain fetch failed: {chain_res.text}"

        return chain_res.json().get("data", []), current_expiry, None

    except Exception as e:
        return None, None, f"Exception during data fetch: {e}"

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
        .btn-warning { background-color: #ffc107; color: #212529; }
        .btn:hover { opacity: 0.9; }
        table { width: 100%; border-collapse: collapse; background: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        th, td { border: 1px solid #e0e0e0; padding: 9px; text-align: center; font-size: 14px; }
        th { background-color: #007bff; color: white; }
        tr:nth-child(even){ background-color: #f8f9fa; }
        .atm { background-color: #fff3cd !important; font-weight: bold; color: #856404; }
        .ce-header { background-color: #28a745 !important; }
        .pe-header { background-color: #dc3545 !important; }
        .error-msg { color: #856404; font-weight: bold; padding: 12px; background: #fff3cd; border: 1px solid #ffeeba; border-radius: 4px; margin-top: 15px; }
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
        <a href="/login" class="btn btn-warning">Login with Upstox</a>
    </div>

    {% if error %}
        <div class="error-msg">
            {{ error }}<br><br>
            <a href="/login" class="btn btn-warning">Click Here to Login & Activate Token</a>
        </div>
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
    output.headers["Content-Disposition"] =
