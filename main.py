import os
import time
import io
import csv
import json
import threading
import urllib.parse
import requests
import pyotp
from flask import Flask, render_template_string, request, Response

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
    "cooldown_duration": 1500  # 25-minute lockout on failure
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
        print(f"[Session Warning] Read error: {e}")
    return None

def save_session_token(token):
    try:
        with open(SESSION_FILE_PATH, "w") as f:
            json.dump({"access_token": token, "generated_at": time.time()}, f)
    except Exception as e:
        print(f"[Session Warning] Write error: {e}")

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
            print(f"[Auth Security Lock] Cooldown active! Blocked for {remaining}s.")
            return None

        if not all([CLIENT_ID, USER_ID, PIN, TOTP_KEY]):
            print("[Auth Error] Missing required environment variables.")
            return None

        try:
            clean_totp_key = str(TOTP_KEY).strip().upper().replace(" ", "").replace("-", "")
            totp_code = str(pyotp.TOTP(clean_totp_key).now()).zfill(6)
            clean_pin = str(PIN).strip()
            clean_client_id = str(CLIENT_ID).strip()

            totp_url = "https://api.upstox.com/v2/totp-login/authorization/token"
            headers = {"accept": "application/json", "Content-Type": "application/json", "x-api-key": clean_client_id}
            payload = {"totp": totp_code, "pin": clean_pin}

            res = requests.post(totp_url, json=payload, headers=headers, timeout=10)
            if res.status_code == 200:
                json_data = res.json()
                token = json_data.get("access_token") or json_data.get("data", {}).get("access_token")
                if token:
                    save_session_token(token)
                    AUTH_COOLDOWN["last_failure_time"] = 0
                    return token

            AUTH_COOLDOWN["last_failure_time"] = time.time()
        except Exception as e:
            print(f"[Auth Exception] {e}")
            AUTH_COOLDOWN["last_failure_time"] = time.time()

        return None

def get_valid_token():
    token = load_session_token()
    if token:
        return token
    return perform_single_auth()

# --- UPSTOX DATA FETCHING & ANALYSIS ---
def fetch_option_chain(symbol):
    token = get_valid_token()
    if not token:
        return None, None, "Authentication paused. Check server logs or wait 25 mins."

    instrument_key = INSTRUMENT_KEYS.get(symbol, "NSE_INDEX|Nifty 50")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    try:
        expiry_url = f"https://api.upstox.com/v2/option/contract?instrument_key={urllib.parse.quote(instrument_key)}"
        exp_res = requests.get(expiry_url, headers=headers, timeout=10)

        if exp_res.status_code == 401:
            invalidate_session()
            token = perform_single_auth()
            if not token:
                return None, None, "Session expired and re-authentication paused."
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

        return chain_res.json().get("data", []), current_expiry, None
    except Exception as e:
        return None, None, f"Exception: {e}"

def process_option_data(symbol):
    chain_data, current_expiry, error = fetch_option_chain(symbol)
    updated_time = time.strftime("%m/%d/%Y %H:%M:%S")

    if error or not chain_data:
        return {
            "rows": [], "spot_price": 0, "expiry": current_expiry or "N/A", "updated": updated_time,
            "pcr": 0, "total_ce_oi": 0, "total_pe_oi": 0, "max_ce_oi": 1, "max_pe_oi": 1,
            "sentiment_pct": 50, "sentiment_label": "Neutral", "max_pain_strike": 0,
            "top_resistance": 0, "top_support": 0, "error": error or "No option data available."
        }

    spot_price = chain_data[0].get("underlying_spot_price", 0) if chain_data else 0
    range_limit = 1200 if symbol == "BANKNIFTY" else 600
    rows = []

    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_vol = 0
    total_pe_vol = 0
    max_ce_oi = 1
    max_pe_oi = 1
    
    max_ce_oi_strike = 0
    max_pe_oi_strike = 0

    # Max Pain calculation matrix
    strike_losses = {}

    for item in chain_data:
        strike = item.get("strike_price", 0)
        ce = item.get("call_options", {}) or {}
        pe = item.get("put_options", {}) or {}

        ce_mkt = ce.get("market_data", {}) or {}
        ce_gk = ce.get("option_greeks", {}) or {}
        pe_mkt = pe.get("market_data", {}) or {}
        pe_gk = pe.get("option_greeks", {}) or {}

        c_oi = ce_mkt.get("oi", 0) or 0
        p_oi = pe_mkt.get("oi", 0) or 0
        c_vol = ce_mkt.get("volume", 0) or 0
        p_vol = pe_mkt.get("volume", 0) or 0

        total_ce_oi += c_oi
        total_pe_oi += p_oi
        total_ce_vol += c_vol
        total_pe_vol += p_vol

        if c_oi > max_ce_oi:
            max_ce_oi = c_oi
            max_ce_oi_strike = strike
        if p_oi > max_pe_oi:
            max_pe_oi = p_oi
            max_pe_oi_strike = strike

        # Max Pain accumulation
        strike_losses[strike] = 0
        for inner in chain_data:
            s = inner.get("strike_price", 0)
            c_inner_oi = (inner.get("call_options", {}) or {}).get("market_data", {}).get("oi", 0) or 0
            p_inner_oi = (inner.get("put_options", {}) or {}).get("market_data", {}).get("oi", 0) or 0
            
            if strike > s:
                strike_losses[strike] += (strike - s) * c_inner_oi
            elif strike < s:
                strike_losses[strike] += (s - strike) * p_inner_oi

        if abs(strike - spot_price) <= range_limit:
            rows.append({
                "strike": strike,
                "ce_oi": c_oi,
                "ce_vol": c_vol,
                "ce_iv": round(ce_gk.get("iv", 0) or 0, 2),
                "ce_ltp": ce_mkt.get("ltp", 0) or 0,
                "ce_bid": ce_mkt.get("bid_price", 0) or 0,
                "ce_ask": ce_mkt.get("ask_price", 0) or 0,
                "ce_delta": round(ce_gk.get("delta", 0) or 0, 2),
                "ce_theta": round(ce_gk.get("theta", 0) or 0, 2),
                "ce_vega": round(ce_gk.get("vega", 0) or 0, 2),
                "ce_gamma": round(ce_gk.get("gamma", 0) or 0, 4),
                "pe_ltp": pe_mkt.get("ltp", 0) or 0,
                "pe_bid": pe_mkt.get("bid_price", 0) or 0,
                "pe_ask": pe_mkt.get("ask_price", 0) or 0,
                "pe_iv": round(pe_gk.get("iv", 0) or 0, 2),
                "pe_vol": p_vol,
                "pe_oi": p_oi,
                "pe_delta": round(pe_gk.get("delta", 0) or 0, 2),
                "pe_theta": round(pe_gk.get("theta", 0) or 0, 2),
                "pe_vega": round(pe_gk.get("vega", 0) or 0, 2),
                "pe_gamma": round(pe_gk.get("gamma", 0) or 0, 4),
            })

    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0
    max_pain_strike = min(strike_losses, key=strike_losses.get) if strike_losses else spot_price

    # Multi-factor Sentiment Calculation (0 to 100%)
    pcr_factor = min(max(pcr / 2.0, 0.0), 1.0) * 50
    vol_ratio = (total_pe_vol / (total_ce_vol + 1))
    vol_factor = min(max(vol_ratio / 2.0, 0.0), 1.0) * 50
    sentiment_pct = round(pcr_factor + vol_factor, 1)

    if sentiment_pct >= 60:
        sentiment_label = "Strong Bullish"
    elif sentiment_pct >= 52:
        sentiment_label = "Mildly Bullish"
    elif sentiment_pct <= 40:
        sentiment_label = "Strong Bearish"
    elif sentiment_pct <= 48:
        sentiment_label = "Mildly Bearish"
    else:
        sentiment_label = "Neutral"

    return {
        "rows": rows,
        "spot_price": spot_price,
        "expiry": current_expiry,
        "updated": updated_time,
        "pcr": pcr,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "max_ce_oi": max_ce_oi,
        "max_pe_oi": max_pe_oi,
        "sentiment_pct": sentiment_pct,
        "sentiment_label": sentiment_label,
        "max_pain_strike": max_pain_strike,
        "top_resistance": max_ce_oi_strike,
        "top_support": max_pe_oi_strike,
        "error": None
    }

# --- DASHBOARD HTML TEMPLATE ---
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Upstox Option Chain & Trend Dashboard</title>
    <meta http-equiv="refresh" content="60">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 15px; background-color: #f4f6f9; }
        h2 { color: #1a252f; margin-bottom: 12px; }
        .info-box { background: #fff; padding: 18px; border-radius: 8px; margin-bottom: 18px; box-shadow: 0 2px 6px rgba(0,0,0,0.08); }
        .metrics-grid { display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 15px; font-size: 14px; }
        .metric-card { background: #f8f9fa; padding: 10px 14px; border-radius: 6px; border-left: 4px solid #007bff; }
        .pcr-bullish { border-left-color: #28a745; color: #1e7e34; }
        .pcr-bearish { border-left-color: #dc3545; color: #bd2130; }
        .btn { display: inline-block; padding: 8px 16px; color: #fff; background-color: #007bff; text-decoration: none; border-radius: 4px; margin-right: 8px; font-weight: 500; font-size: 13px; }
        .btn-success { background-color: #28a745; }
        .btn-secondary { background-color: #6c757d; }
        
        /* Trend Meter Bar */
        .sentiment-container { margin: 12px 0; }
        .sentiment-bar-bg { background-color: #e9ecef; border-radius: 10px; height: 18px; width: 100%; overflow: hidden; display: flex; }
        .sentiment-bar-fill { height: 100%; transition: width 0.5s ease; }
        
        /* Dropdown Controls */
        .control-panel { display: flex; align-items: center; justify-content: space-between; margin-bottom: 15px; background: #fff; padding: 12px 18px; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.06); }
        select { padding: 6px 12px; font-size: 14px; border-radius: 4px; border: 1px solid #ced4da; }

        /* Inline Bars */
        .bar-container { display: flex; align-items: center; gap: 6px; }
        .bar-ce { background-color: #ffc9c9; height: 12px; border-radius: 2px; }
        .bar-pe { background-color: #b2f2bb; height: 12px; border-radius: 2px; }

        .table-container { overflow-x: auto; background: #fff; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.06); }
        table { width: 100%; border-collapse: collapse; min-width: 1200px; }
        th, td { border: 1px solid #e9ecef; padding: 7px 9px; text-align: center; font-size: 13px; }
        th { background-color: #343a40; color: white; position: sticky; top: 0; }
        tr:nth-child(even){ background-color: #f8f9fa; }
        .atm { background-color: #fff3cd !important; font-weight: bold; color: #856404; font-size: 14px; }
        .ce-header { background-color: #28a745 !important; }
        .pe-header { background-color: #dc3545 !important; }
        .error-msg { color: #dc3545; font-weight: bold; padding: 12px; background: #f8d7da; border-radius: 4px; }
    </style>
    <script>
        function updateViewMode() {
            var mode = document.getElementById("viewMode").value;
            var allCols = ["oi", "greeks", "orderbook"];
            allCols.forEach(function(col) {
                var elems = document.getElementsByClassName("mode-" + col);
                for (var i = 0; i < elems.length; i++) {
                    elems[i].style.display = (mode === "all" || mode === col) ? "" : "none";
                }
            });
        }
    </script>
</head>
<body>
    <h2>Upstox Pro Option Chain & Live Trend Dashboard</h2>
    <div class="info-box">
        <div class="metrics-grid">
            <div class="metric-card"><strong>Symbol:</strong> {{ data.symbol }}</div>
            <div class="metric-card"><strong>Spot Price:</strong> {{ data.spot_price }}</div>
            <div class="metric-card"><strong>Expiry:</strong> {{ data.expiry }}</div>
            <div class="metric-card {% if data.pcr >= 1.0 %}pcr-bullish{% else %}pcr-bearish{% endif %}">
                <strong>PCR:</strong> {{ data.pcr }} ({% if data.pcr >= 1.0 %}Bullish{% else %}Bearish{% endif %})
            </div>
            <div class="metric-card"><strong>Max Pain:</strong> {{ data.max_pain_strike }}</div>
            <div class="metric-card"><strong>Resistance:</strong> {{ data.top_resistance }}</div>
            <div class="metric-card"><strong>Support:</strong> {{ data.top_support }}</div>
            <div class="metric-card"><strong>Updated:</strong> {{ data.updated }}</div>
        </div>

        <div class="sentiment-container">
            <strong>Intraday Sentiment Index: {{ data.sentiment_pct }}% ({{ data.sentiment_label }})</strong>
            <div class="sentiment-bar-bg" style="margin-top:5px;">
                <div class="sentiment-bar-fill" style="width: {{ data.sentiment_pct }}%; background-color: {% if data.sentiment_pct >= 55 %}#28a745{% elif data.sentiment_pct <= 45 %}#dc3545{% else %}#ffc107{% endif %};"></div>
            </div>
        </div>
    </div>

    <div class="control-panel">
        <div>
            <a href="/?symbol=NIFTY" class="btn">View NIFTY</a>
            <a href="/?symbol=BANKNIFTY" class="btn">View BANKNIFTY</a>
            <a href="/analytics?symbol={{ data.symbol }}" class="btn btn-secondary">Dedicated Analytics Page</a>
            <a href="/download?symbol={{ data.symbol }}" class="btn btn-success">Download CSV</a>
        </div>
        <div>
            <label for="viewMode"><strong>Select Display Columns: </strong></label>
            <select id="viewMode" onchange="updateViewMode()">
                <option value="all">Show All Columns</option>
                <option value="oi" selected>Open Interest & Volume (Histograms)</option>
                <option value="greeks">Option Greeks Only</option>
                <option value="orderbook">Order Book Depth (Bid/Ask)</option>
            </select>
        </div>
    </div>

    {% if data.error %}
        <div class="error-msg">Error: {{ data.error }}</div>
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
                    <th class="mode-oi">CE OI Histogram</th>
                    <th class="mode-oi">Vol</th>
                    <th class="mode-greeks">IV</th>
                    <th class="mode-greeks">Delta</th>
                    <th class="mode-greeks">Theta</th>
                    <th class="mode-greeks">Vega</th>
                    <th class="mode-greeks">Gamma</th>
                    <th class="mode-orderbook">Bid</th>
                    <th class="mode-orderbook">Ask</th>
                    <th>LTP</th>
                    
                    <th>Price</th>
                    
                    <th>LTP</th>
                    <th class="mode-orderbook">Bid</th>
                    <th class="mode-orderbook">Ask</th>
                    <th class="mode-greeks">Delta</th>
                    <th class="mode-greeks">Theta</th>
                    <th class="mode-greeks">Vega</th>
                    <th class="mode-greeks">Gamma</th>
                    <th class="mode-greeks">IV</th>
                    <th class="mode-oi">Vol</th>
                    <th class="mode-oi">PE OI Histogram</th>
                </tr>
            </thead>
            <tbody>
                {% for r in data.rows %}
                <tr>
                    <!-- CE Columns -->
                    <td class="mode-oi">
                        <div class="bar-container" style="justify-content: flex-end;">
                            <span>{{ "{:,}".format(r.ce_oi) }}</span>
                            <div class="bar-ce" style="width: {{ (r.ce_oi / data.max_ce_oi * 80)|round|int }}px;"></div>
                        </div>
                    </td>
                    <td class="mode-oi">{{ "{:,}".format(r.ce_vol) }}</td>
                    <td class="mode-greeks">{{ r.ce_iv }}</td>
                    <td class="mode-greeks">{{ r.ce_delta }}</td>
                    <td class="mode-greeks">{{ r.ce_theta }}</td>
                    <td class="mode-greeks">{{ r.ce_vega }}</td>
                    <td class="mode-greeks">{{ r.ce_gamma }}</td>
                    <td class="mode-orderbook">{{ r.ce_bid }}</td>
                    <td class="mode-orderbook">{{ r.ce_ask }}</td>
                    <td><strong>{{ r.ce_ltp }}</strong></td>

                    <!-- Strike -->
                    <td class="atm">{{ r.strike }}</td>

                    <!-- PE Columns -->
                    <td><strong>{{ r.pe_ltp }}</strong></td>
                    <td class="mode-orderbook">{{ r.pe_bid }}</td>
                    <td class="mode-orderbook">{{ r.pe_ask }}</td>
                    <td class="mode-greeks">{{ r.pe_delta }}</td>
                    <td class="mode-greeks">{{ r.pe_theta }}</td>
                    <td class="mode-greeks">{{ r.pe_vega }}</td>
                    <td class="mode-greeks">{{ r.pe_gamma }}</td>
                    <td class="mode-greeks">{{ r.pe_iv }}</td>
                    <td class="mode-oi">{{ "{:,}".format(r.pe_vol) }}</td>
                    <td class="mode-oi">
                        <div class="bar-container" style="justify-content: flex-start;">
                            <div class="bar-pe" style="width: {{ (r.pe_oi / data.max_pe_oi * 80)|round|int }}px;"></div>
                            <span>{{ "{:,}".format(r.pe_oi) }}</span>
                        </div>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% endif %}

    <script>
        updateViewMode();
    </script>
</body>
</html>
"""

# --- DEDICATED ANALYTICS HTML TEMPLATE ---
ANALYTICS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Option Chain Trend Analytics</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #f4f6f9; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin-top: 20px; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.08); }
        .card h3 { margin-top: 0; color: #343a40; font-size: 16px; }
        .big-stat { font-size: 28px; font-weight: bold; margin: 10px 0; color: #007bff; }
        .btn { display: inline-block; padding: 8px 16px; color: #fff; background-color: #007bff; text-decoration: none; border-radius: 4px; }
    </style>
</head>
<body>
    <h2>Detailed Intraday Analytics & Levels ({{ data.symbol }})</h2>
    <a href="/?symbol={{ data.symbol }}" class="btn">Back to Main Dashboard</a>
    
    <div class="grid">
        <div class="card">
            <h3>Intraday Sentiment</h3>
            <div class="big-stat" style="color: {% if data.sentiment_pct >= 55 %}#28a745{% elif data.sentiment_pct <= 45 %}#dc3545{% else %}#ffc107{% endif %};">
                {{ data.sentiment_pct }}%
            </div>
            <p><strong>Bias:</strong> {{ data.sentiment_label }}</p>
        </div>
        <div class="card">
            <h3>Put-Call Ratio (PCR)</h3>
            <div class="big-stat">{{ data.pcr }}</div>
            <p><strong>Total Call OI:</strong> {{ "{:,}".format(data.total_ce_oi) }}</p>
            <p><strong>Total Put OI:</strong> {{ "{:,}".format(data.total_pe_oi) }}</p>
        </div>
        <div class="card">
            <h3>Max Pain Strike</h3>
            <div class="big-stat" style="color: #6f42c1;">{{ data.max_pain_strike }}</div>
            <p>Estimated expiry level maximizing option sellers' profit.</p>
        </div>
        <div class="card">
            <h3>Key Reversal Levels</h3>
            <p><strong>Major Resistance:</strong> {{ data.top_resistance }}</p>
            <p><strong>Major Support:</strong> {{ data.top_support }}</p>
        </div>
    </div>
</body>
</html>
"""

# --- ROUTES ---
@app.route("/health")
def health():
    return "OK", 200

@app.route("/", methods=["GET", "HEAD"])
def index():
    if request.method == "HEAD":
        return Response(status=200)

    symbol = request.args.get("symbol", "NIFTY").upper()
    data = process_option_data(symbol)
    data["symbol"] = symbol
    return render_template_string(DASHBOARD_TEMPLATE, data=data)

@app.route("/analytics")
def analytics():
    symbol = request.args.get("symbol", "NIFTY").upper()
    data = process_option_data(symbol)
    data["symbol"] = symbol
    return render_template_string(ANALYTICS_TEMPLATE, data=data)

@app.route("/download")
def download_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    data = process_option_data(symbol)

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([f"Symbol: {symbol}", f"Spot Price: {data['spot_price']}", f"Expiry: {data['expiry']}", f"PCR: {data['pcr']}", f"Updated: {data['updated']}"])
    cw.writerow([
        "CE OI", "CE Vol", "CE IV", "CE Delta", "CE Theta", "CE Vega", "CE Gamma", "CE Bid", "CE Ask", "CE LTP",
        "STRIKE",
        "PE LTP", "PE Bid", "PE Ask", "PE Delta", "PE Theta", "PE Vega", "PE Gamma", "PE IV", "PE Vol", "PE OI"
    ])

    if not data["error"]:
        for r in data["rows"]:
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
