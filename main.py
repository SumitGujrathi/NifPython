import os
import time
import io
import csv
import json
import threading
import urllib.parse
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

# --- UPSTOX DATA FETCHING ---
def fetch_option_chain(symbol):
    token = get_valid_token()
    if not token:
        return None, None, "Authentication paused to protect account from lockout. Wait 25 mins or check logs."

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

        chain_data = chain_res.json().get("data", [])
        return chain_data, current_expiry, None

    except Exception as e:
        return None, None, f"Exception during fetch: {e}"

def process_option_data(symbol):
    chain_data, current_expiry, error = fetch_option_chain(symbol)
    updated_time = time.strftime("%m/%d/%Y %H:%M:%S")

    if error or not chain_data:
        return [], 0, current_expiry or "N/A", updated_time, 0, 0, 0, error or "No option chain data available."

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
        
        c_oi_chg = ce_mkt.get("poi", 0) or (c_oi - ce_mkt.get("prev_oi", c_oi))
        p_oi_chg = pe_mkt.get("poi", 0) or (p_oi - pe_mkt.get("prev_oi", p_oi))
        
        c_bid_qty = ce_mkt.get("bid_qty", 0) or 0
        c_ask_qty = ce_mkt.get("ask_qty", 0) or 0
        p_bid_qty = pe_mkt.get("bid_qty", 0) or 0
        p_ask_qty = pe_mkt.get("ask_qty", 0) or 0

        total_ce_oi += c_oi
        total_pe_oi += p_oi

        if abs(strike - spot_price) <= range_limit:
            rows.append({
                "strike": strike,
                "ce_oi": c_oi,
                "ce_oi_chg": c_oi_chg,
                "ce_vol": ce_mkt.get("volume", 0) or 0,
                "ce_iv": round(ce_greek.get("iv", 0) or 0, 2),
                "ce_ltp": ce_mkt.get("ltp", 0) or 0,
                "ce_bid": ce_mkt.get("bid_price", 0) or 0,
                "ce_ask": ce_mkt.get("ask_price", 0) or 0,
                "ce_bid_qty": c_bid_qty,
                "ce_ask_qty": c_ask_qty,
                "ce_delta": round(ce_greek.get("delta", 0) or 0, 2),
                "ce_theta": round(ce_greek.get("theta", 0) or 0, 2),
                "ce_vega": round(ce_greek.get("vega", 0) or 0, 2),
                "ce_gamma": round(ce_greek.get("gamma", 0) or 0, 4),
                "pe_ltp": pe_mkt.get("ltp", 0) or 0,
                "pe_bid": pe_mkt.get("bid_price", 0) or 0,
                "pe_ask": pe_mkt.get("ask_price", 0) or 0,
                "pe_bid_qty": p_bid_qty,
                "pe_ask_qty": p_ask_qty,
                "pe_iv": round(pe_greek.get("iv", 0) or 0, 2),
                "pe_vol": pe_mkt.get("volume", 0) or 0,
                "pe_oi": p_oi,
                "pe_oi_chg": p_oi_chg,
                "pe_delta": round(pe_greek.get("delta", 0) or 0, 2),
                "pe_theta": round(pe_greek.get("theta", 0) or 0, 2),
                "pe_vega": round(pe_greek.get("vega", 0) or 0, 2),
                "pe_gamma": round(pe_greek.get("gamma", 0) or 0, 4),
            })

    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0
    return rows, spot_price, current_expiry, updated_time, pcr, total_ce_oi, total_pe_oi, None

# --- ORIGINAL UNTOUCHED HTML MAIN TABLE TEMPLATE ---
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
    <h2>Upstox Pro Option Chain & Greeks Dashboard made by Sumit Gujrathi</h2>
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
            <a href="/analysis?symbol={{ symbol }}" class="btn btn-purple">📊 Open Analysis Dashboard</a>
            <a href="/download?symbol={{ symbol }}" class="btn btn-success">Download Full CSV</a>
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

# --- ENHANCED ANALYSIS DASHBOARD TEMPLATE (AJAX LIVE AUTO-FETCH & PERSISTENT DROPDOWN) ---
ANALYSIS_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ symbol }} Option Chain Analysis Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
        .chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .chart-title { font-size: 18px; font-weight: bold; color: #f8fafc; }
        .timer-badge { background: #0284c7; color: #ffffff; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .error-msg { color: #f87171; font-weight: bold; padding: 15px; background: #450a0a; border-radius: 8px; }
    </style>
</head>
<body>
    <h2>📊 {{ symbol }} Intraday Options Analysis Dashboard</h2>
    <div class="subtitle">Live background auto-sync — Zero page reloads & zero scroll loss</div>

    <div class="nav-bar">
        <label style="color: #94a3b8; font-weight: bold;">Symbol:</label>
        <select id="symbolSelector" onchange="switchSymbol(this.value)">
            <option value="NIFTY" {% if symbol == 'NIFTY' %}selected{% endif %}>NIFTY 50</option>
            <option value="BANKNIFTY" {% if symbol == 'BANKNIFTY' %}selected{% endif %}>BANK NIFTY</option>
        </select>

        <label style="color: #94a3b8; font-weight: bold; margin-left: 15px;">Metric View:</label>
        <select id="metricSelector" onchange="updateChartMetric()">
            <option value="oi">Open Interest (CE vs PE)</option>
            <option value="oi_chg">Change in Open Interest (Chg in OI)</option>
            <option value="vol">Trading Volume</option>
            <option value="depth_qty">Order Book Depth (Bid Quantity vs Ask Quantity)</option>
            <option value="delta">Delta Exposure</option>
            <option value="theta">Theta Time Decay</option>
        </select>

        <div style="margin-left: auto; display: flex; align-items: center; gap: 12px;">
            <span class="timer-badge">Auto Sync: <span id="countdown">10</span>s</span>
            <a href="/?symbol={{ symbol }}" class="btn btn-table">📋 Back to Table View</a>
        </div>
    </div>

    <div id="errorContainer" class="error-msg" style="display: none;"></div>

    <div class="metrics-grid">
        <div class="card" style="border-left-color: #38bdf8;">
            <div class="card-title">Spot Price</div>
            <div class="card-value" id="cardSpot">{{ spot_price }}</div>
        </div>
        <div class="card" id="cardPcrBox">
            <div class="card-title">Put-Call Ratio (PCR)</div>
            <div class="card-value" id="cardPcr">{{ pcr }}</div>
        </div>
        <div class="card" style="border-left-color: #ef4444;">
            <div class="card-title">Major Resistance (Max Call OI)</div>
            <div class="card-value" id="maxCallStrike">-</div>
        </div>
        <div class="card" style="border-left-color: #22c55e;">
            <div class="card-title">Major Support (Max Put OI)</div>
            <div class="card-value" id="maxPutStrike">-</div>
        </div>
        <div class="card" style="border-left-color: #a855f7;">
            <div class="card-title">Last Updated</div>
            <div class="card-value" id="cardUpdated" style="font-size: 16px;">{{ updated }}</div>
        </div>
    </div>

    <!-- DYNAMIC METRIC SENTIMENT GAUGE (CE VS PE SUM & AVG) -->
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

    <!-- MAIN GRAPHICAL BAR CHART -->
    <div class="chart-box">
        <div class="chart-header">
            <div class="chart-title" id="chartHeaderTitle">Strike-Wise Open Interest (Call vs Put)</div>
        </div>
        <canvas id="analysisChart" style="max-height: 480px;"></canvas>
    </div>

    <script>
        let currentSymbol = "{{ symbol }}";
        let rawRows = {{ rows | tojson }};
        let myChart = null;

        // Restore selected metric from localStorage if present
        const savedMetric = localStorage.getItem("selected_option_metric");
        if (savedMetric) {
            const selector = document.getElementById("metricSelector");
            if (selector.querySelector(`option[value="${savedMetric}"]`)) {
                selector.value = savedMetric;
            }
        }

        function switchSymbol(newSym) {
            window.location.href = `/analysis?symbol=${newSym}`;
        }

        function renderPageData(data) {
            if (data.error) {
                document.getElementById("errorContainer").style.display = "block";
                document.getElementById("errorContainer").innerText = "Error: " + data.error;
                return;
            }
            document.getElementById("errorContainer").style.display = "none";

            rawRows = data.rows || [];
            document.getElementById("cardSpot").innerText = data.spot_price || 0;
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

            // Resistance & Support strikes
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
                        { label: 'Calls', data: [], backgroundColor: 'rgba(239, 68, 68, 0.85)' },
                        { label: 'Puts', data: [], backgroundColor: 'rgba(34, 197, 94, 0.85)' }
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
                        legend: { labels: { color: '#f8fafc', font: { size: 14 } } },
                        tooltip: { mode: 'index', intersect: false }
                    }
                }
            });
        }

        function updateChartMetric() {
            const selected = document.getElementById("metricSelector").value;
            localStorage.setItem("selected_option_metric", selected);

            const titleEl = document.getElementById("chartHeaderTitle");
            const strikes = rawRows.map(r => r.strike);

            let ceData = [], peData = [];
            let ceLabel = "", peLabel = "";
            let ceColor = "", peColor = "";
            let metricTitle = "";

            if (selected === "oi") {
                metricTitle = "Open Interest (CE vs PE)";
                ceLabel = "Call OI"; peLabel = "Put OI";
                ceColor = 'rgba(239, 68, 68, 0.85)'; peColor = 'rgba(34, 197, 94, 0.85)';
                ceData = rawRows.map(r => r.ce_oi);
                peData = rawRows.map(r => r.pe_oi);
            }
            else if (selected === "oi_chg") {
                metricTitle = "Change in Open Interest (Chg in OI)";
                ceLabel = "Call Chg in OI"; peLabel = "Put Chg in OI";
                ceColor = 'rgba(225, 29, 72, 0.85)'; peColor = 'rgba(16, 185, 129, 0.85)';
                ceData = rawRows.map(r => r.ce_oi_chg);
                peData = rawRows.map(r => r.pe_oi_chg);
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

            myChart.update();

            // Calculate Gauge SUM and AVG
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

        // --- BACKGROUND AJAX POLLING (10s TIMER) ---
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
            fetch(`/api/data?symbol=${currentSymbol}`)
                .then(res => res.json())
                .then(data => {
                    renderPageData(data);
                })
                .catch(err => {
                    console.error("Auto Sync Error:", err);
                });
        }

        // Initialize page
        initChart();
        renderPageData({
            rows: rawRows,
            spot_price: {{ spot_price }},
            expiry: "{{ expiry }}",
            updated: "{{ updated }}",
            pcr: {{ pcr }}
        });
        startAutoSync();
    </script>
</body>
</html>
"""

# --- ROUTES ---
@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/data")
def api_data():
    """Background JSON API endpoint for zero-reload live updates."""
    symbol = request.args.get("symbol", "NIFTY").upper()
    rows, spot_price, expiry, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol)
    return jsonify({
        "rows": rows,
        "spot_price": spot_price,
        "expiry": expiry,
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
    rows, spot_price, expiry, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol)
    return render_template_string(
        MAIN_HTML_TEMPLATE,
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

@app.route("/analysis", methods=["GET"])
def analysis_dashboard():
    symbol = request.args.get("symbol", "NIFTY").upper()
    rows, spot_price, expiry, updated, pcr, total_ce_oi, total_pe_oi, error = process_option_data(symbol)

    return render_template_string(
        ANALYSIS_HTML_TEMPLATE,
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
