import os
import time
import requests
import io
import csv
from flask import Flask, render_template_string, request, Response

app = Flask(__name__)

def fetch_nse_option_chain(symbol):
    base_url = "https://www.nseindia.com"
    api_url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.nseindia.com/option-chain"
    }

    session = requests.Session()
    session.headers.update(headers)

    try:
        session.get(base_url, timeout=10)
        time.sleep(1)
        response = session.get(api_url, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"[{symbol}] Fetch error: {e}")
    return None

def get_processed_data(symbol):
    data = fetch_nse_option_chain(symbol)
    if not data or "records" not in data:
        return [], 0, "N/A", time.strftime("%m/%d/%Y %H:%M:%S")

    underlying_val = data["records"].get("underlyingValue", 0)
    expiry_dates = data["records"].get("expiryDates", [])
    current_expiry = expiry_dates[0] if expiry_dates else "N/A"
    updated_time = time.strftime("%m/%d/%Y %H:%M:%S")

    rows = []
    range_limit = 1200 if symbol == "BANKNIFTY" else 600
    filtered_data = [d for d in data["records"]["data"] if d.get("expiryDate") == current_expiry]

    for item in filtered_data:
        strike = item.get("strikePrice", 0)
        if abs(strike - underlying_val) <= range_limit:
            ce = item.get("CE", {})
            pe = item.get("PE", {})
            rows.append({
                "ce_oi": ce.get("openInterest", 0),
                "ce_vol": ce.get("totalTradedVolume", 0),
                "ce_iv": ce.get("impliedVolatility", 0),
                "ce_ltp": ce.get("lastPrice", 0),
                "strike": strike,
                "pe_ltp": pe.get("lastPrice", 0),
                "pe_iv": pe.get("impliedVolatility", 0),
                "pe_vol": pe.get("totalTradedVolume", 0),
                "pe_oi": pe.get("openInterest", 0)
            })

    return rows, underlying_val, current_expiry, updated_time

# HTML Dashboard Template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>NSE Option Chain Viewer</title>
    <meta http-equiv="refresh" content="60">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f4f6f9; }
        h2 { color: #333; }
        .info-box { background: #fff; padding: 15px; border-radius: 5px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .btn { display: inline-block; padding: 8px 15px; color: #fff; background-color: #007bff; text-decoration: none; border-radius: 4px; margin-right: 10px; }
        .btn-success { background-color: #28a745; }
        table { width: 100%; border-collapse: collapse; background: #fff; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: center; }
        th { background-color: #007bff; color: white; }
        tr:nth-child(even){ background-color: #f9f9f9; }
        .atm { background-color: #fff3cd !important; font-weight: bold; }
    </style>
</head>
<body>
    <h2>NSE Option Chain Dashboard</h2>
    <div class="info-box">
        <strong>Symbol:</strong> {{ symbol }} | 
        <strong>Underlying Price:</strong> {{ ltp }} | 
        <strong>Expiry:</strong> {{ expiry }} | 
        <strong>Updated:</strong> {{ updated }}
        <br><br>
        <a href="/?symbol=NIFTY" class="btn">View NIFTY</a>
        <a href="/?symbol=BANKNIFTY" class="btn">View BANKNIFTY</a>
        <a href="/download?symbol={{ symbol }}" class="btn btn-success">Download CSV</a>
    </div>

    <table>
        <thead>
            <tr>
                <th colspan="4" style="background-color: #28a745;">CALLS</th>
                <th>STRIKE</th>
                <th colspan="4" style="background-color: #dc3545;">PUTS</th>
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
</body>
</html>
"""

@app.route("/")
def index():
    symbol = request.args.get("symbol", "NIFTY").upper()
    rows, ltp, expiry, updated = get_processed_data(symbol)
    return render_template_string(HTML_TEMPLATE, rows=rows, symbol=symbol, ltp=ltp, expiry=expiry, updated=updated)

@app.route("/download")
def download_csv():
    symbol = request.args.get("symbol", "NIFTY").upper()
    rows, ltp, expiry, updated = get_processed_data(symbol)

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow([f"Symbol: {symbol}", f"Underlying LTP: {ltp}", f"Expiry: {expiry}", f"Updated: {updated}"])
    cw.writerow(["CALLS OI", "CALLS Vol", "CALLS IV", "CALLS LTP", "STRIKE", "PUTS LTP", "PUTS IV", "PUTS Vol", "PUTS OI"])

    for r in rows:
        cw.writerow([r["ce_oi"], r["ce_vol"], r["ce_iv"], r["ce_ltp"], r["strike"], r["pe_ltp"], r["pe_iv"], r["pe_vol"], r["pe_oi"]])

    output = Response(si.getvalue(), mimetype="text/csv")
    output.headers["Content-Disposition"] = f"attachment; filename={symbol}_Option_Chain.csv"
    return output

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
