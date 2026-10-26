import os
import time
import io
import csv
import cloudscraper
from flask import Flask, render_template_string, request, Response

app = Flask(__name__)

def fetch_nse_option_chain(symbol):
    """
    Fetches Option Chain JSON from NSE India using Cloudscraper to bypass Akamai bot detection.
    """
    base_url = "https://www.nseindia.com"
    api_url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"

    # Initialize cloudscraper session to mimic real browser TLS fingerprints
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive"
    }

    try:
        # Step 1: Establish base session & acquire Akamai cookies
        init_res = scraper.get(base_url, headers=headers, timeout=12)
        if init_res.status_code != 200:
            print(f"[{symbol}] Base connection failed with status: {init_res.status_code}")
            return None

        time.sleep(1)  # Brief pause to allow session cookie initialization

        # Step 2: Set API headers with Referer
        api_headers = headers.copy()
        api_headers["Accept"] = "application/json, text/plain, */*"
        api_headers["Referer"] = "https://www.nseindia.com/option-chain"

        # Step 3: Fetch Option Chain JSON
        response = scraper.get(api_url, headers=api_headers, timeout=12)
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"[{symbol}] API request failed with status code: {response.status_code}")

    except Exception as e:
        print(f"[{symbol}] Exception during fetch: {e}")

    return None

def get_processed_data(symbol):
    data = fetch_nse_option_chain(symbol)
    
    if not data or "records" not in data or "data" not in data["records"]:
        return [], 0, "N/A (NSE Request Blocked)", time.strftime("%m/%d/%Y %H:%M:%S")

    underlying_val = data["records"].get("underlyingValue", 0)
    expiry_dates = data["records"].get("expiryDates", [])
    current_expiry = expiry_dates[0] if expiry_dates else "N/A"
    updated_time = time.strftime("%m/%d/%Y %H:%M:%S")

    rows = []
    # Dynamic range limit: +/- 1200 for BANKNIFTY, +/- 600 for NIFTY
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
    <title>NSE Option Chain Live Dashboard</title>
    <meta http-equiv="refresh" content="60">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #f4f6f9; }
        h2 { color: #222; }
        .info-box { background: #fff; padding: 15px; border-radius: 6px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.08); }
        .btn { display: inline-block; padding: 9px 18px; color: #fff; background-color: #007bff; text-decoration: none; border-radius: 4px; margin-right: 10px; font-weight: 500; }
        .btn-success { background-color: #28a745; }
        .btn:hover { opacity: 0.9; }
        table { width: 100%; border-collapse: collapse; background: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        th, td { border: 1px solid #e0e0e0; padding: 10px; text-align: center; font-size: 14px; }
        th { background-color: #007bff; color: white; }
        tr:nth-child(even){ background-color: #f8f9fa; }
        .atm { background-color: #fff3cd !important; font-weight: bold; color: #856404; }
        .ce-header { background-color: #28a745 !important; }
        .pe-header { background-color: #dc3545 !important; }
        .error-msg { color: #dc3545; font-weight: bold; margin-top: 10px; }
    </style>
</head>
<body>
    <h2>NSE Option Chain Live Dashboard</h2>
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

    {% if not rows %}
        <div class="info-box error-msg">
            Unable to pull data from NSE at this moment. The session challenge may be refreshing. Click 'View {{ symbol }}' above to retry.
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
