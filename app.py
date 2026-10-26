import os
import time
import logging
import threading
from datetime import datetime
import pandas as pd
from flask import Flask, Response
from apscheduler.schedulers.background import BackgroundScheduler
from curl_cffi import requests as curl_requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Complete list of dashboard tickers
STOCK_LIST = [
    "NIFTY_50", "NIFTY_BANK", "ACC", "ADANIPORTS", "SBIN", "AMBUJACEM",
    "WIPRO", "APOLLOTYRE", "ASIANPAINT", "AUROPHARMA", "AXISBANK",
    "BAJFINANCE", "IOC", "BANKBARODA", "BATAINDIA", "BERGEPAINT", "BHARATFORG",
    "COALINDIA", "INDUSINDBK", "DRREDDY", "INFY", "JSWSTEEL", "POWERGRID",
    "LICHSGFIN", "CANBK", "MGL", "M&MFIN", "HDFCBANK", "MANAPPURAM", "MARICO",
    "SUNTV", "HINDZINC", "ICICIBANK", "ZEEL"
]

# Mapping internal names to NSE symbols
SYMBOL_MAPPING = {
    "NIFTY_50": "NIFTY 50",
    "NIFTY_BANK": "NIFTY BANK",
    "M&MFIN": "M&MFIN"
}

DATA_CACHE = {"data": pd.DataFrame(), "timestamp": None, "status": "Initializing..."}
CACHE_LOCK = threading.Lock()

def fetch_nse_bulk_data():
    """
    Fetches stock data in BULK from NSE India endpoints using Chrome impersonation.
    Reduces 34 separate requests down to 2 fast requests.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/"
    }

    session = curl_requests.Session(impersonate="chrome")

    # Step 1: Establish session cookies
    try:
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
    except Exception as e:
        logging.warning(f"Failed to load NSE home page for cookies: {e}")

    extracted_data = {}

    # Step 2: Fetch NIFTY 500 Bulk Stock Data in ONE request
    try:
        url_stocks = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500"
        resp = session.get(url_stocks, headers=headers, timeout=10)
        if resp.status_code == 200:
            stocks_json = resp.json().get('data', [])
            for item in stocks_json:
                symbol = item.get('symbol')
                extracted_data[symbol] = {
                    "Current Price / LTP": item.get('lastPrice'),
                    "Open": item.get('open'),
                    "High": item.get('dayHigh'),
                    "Low": item.get('dayLow'),
                    "Previous Close": item.get('previousClose'),
                    "Volume": item.get('totalTradedVolume')
                }
    except Exception as e:
        logging.error(f"Error fetching bulk stocks from NSE: {e}")

    # Step 3: Fetch Indices (NIFTY 50, NIFTY BANK) in ONE request
    try:
        url_indices = "https://www.nseindia.com/api/allIndices"
        resp_idx = session.get(url_indices, headers=headers, timeout=10)
        if resp_idx.status_code == 200:
            indices_json = resp_idx.json().get('data', [])
            for idx in indices_json:
                name = idx.get('index')
                extracted_data[name] = {
                    "Current Price / LTP": idx.get('last'),
                    "Open": idx.get('open'),
                    "High": idx.get('high'),
                    "Low": idx.get('low'),
                    "Previous Close": idx.get('previousClose'),
                    "Volume": None
                }
    except Exception as e:
        logging.error(f"Error fetching indices from NSE: {e}")

    # Step 4: Map extracted data to STOCK_LIST
    final_rows = []
    for stock_name in STOCK_LIST:
        nse_key = SYMBOL_MAPPING.get(stock_name, stock_name)
        data = extracted_data.get(nse_key, {})

        final_rows.append({
            "Symbol": stock_name,
            "Current Price / LTP": data.get("Current Price / LTP"),
            "Open": data.get("Open"),
            "High": data.get("High"),
            "Low": data.get("Low"),
            "Previous Close": data.get("Previous Close"),
            "Volume": data.get("Volume")
        })

    return pd.DataFrame(final_rows)

def background_update_job():
    logging.info("Executing fast bulk refresh from NSE India...")
    df_new = fetch_nse_bulk_data()

    with CACHE_LOCK:
        if not df_new.empty and df_new['Current Price / LTP'].notna().any():
            DATA_CACHE["data"] = df_new
            DATA_CACHE["timestamp"] = datetime.now()
            DATA_CACHE["status"] = "Active (NSE Live)"
            logging.info("Stock cache successfully updated via Bulk API.")
        else:
            DATA_CACHE["status"] = "Retrying NSE bulk query..."
            logging.warning("Bulk fetch returned empty results.")

# --- BACKGROUND SCHEDULER SETUP ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(background_update_job, 'interval', seconds=60)
scheduler.start()

# Run instant fetch on boot
threading.Thread(target=background_update_job, daemon=True).start()

app = Flask(__name__)

def format_dataframe_to_html_table(df):
    if df.empty:
        return '<tr><td colspan="7" style="text-align: center; color: #1976d2; font-weight: bold;">Loading initial data... Please refresh in a few seconds.</td></tr>', []

    formatted_df = df.copy()
    numeric_cols = formatted_df.columns.drop('Symbol')
    formatted_df = formatted_df.dropna(subset=numeric_cols, how='all').reset_index(drop=True)

    if formatted_df.empty:
        return '<tr><td colspan="7" style="text-align: center; color: red;">No stock data available.</td></tr>', []

    for col in ["Current Price / LTP", "Open", "High", "Low", "Previous Close"]:
        numeric_col = pd.to_numeric(formatted_df[col], errors='coerce')
        formatted_df[col] = numeric_col.apply(
            lambda x: "N/A" if pd.isna(x) or x == 0 else f"₹{x:,.2f}"
        )

    numeric_volume = pd.to_numeric(formatted_df["Volume"], errors='coerce')
    formatted_df["Volume"] = numeric_volume.apply(
        lambda x: "N/A" if pd.isna(x) or x == 0 else f"{x:,.0f}"
    )

    headers = formatted_df.columns.tolist()
    header_html = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"

    rows_html = ""
    for _, row in formatted_df.iterrows():
        row_cells = "".join(f"<td>{row[col]}</td>" for col in headers)
        rows_html += f"<tr>{row_cells}</tr>"

    table_html = f"<thead>{header_html}</thead><tbody>{rows_html}</tbody>"
    return table_html, headers

@app.route("/download_csv")
def download_csv():
    with CACHE_LOCK:
        df = DATA_CACHE["data"].copy()

    if df.empty:
        return "Error: Data loading in progress.", 503

    numeric_cols = df.columns.drop('Symbol')
    df_clean = df.dropna(subset=numeric_cols, how='all')

    csv_data = df_clean.to_csv(index=False)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=stock_data_live.csv"}
    )

@app.route("/")
def dashboard():
    with CACHE_LOCK:
        df_data = DATA_CACHE["data"].copy()
        last_updated_dt = DATA_CACHE["timestamp"]
        status_msg = DATA_CACHE["status"]

    table_content_html, _ = format_dataframe_to_html_table(df_data)
    last_updated_str = last_updated_dt.strftime("%Y-%m-%d %H:%M:%S") if last_updated_dt else "Loading..."

    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="60">
    <title>📈 Stock Financial Dashboard</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background-color: #f0f2f6; color: #333; }}
        .container {{ max-width: 1400px; margin: auto; background-color: white; padding: 30px; border-radius: 12px; box-shadow: 0 6px 15px rgba(0, 0, 0, 0.1); }}
        h1 {{ color: #1e88e5; border-bottom: 3px solid #eee; padding-bottom: 10px; text-align: center; }}
        .header-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
        .info-box {{ background-color: #e3f2fd; color: #0d47a1; padding: 10px 15px; border-radius: 6px; font-size: 0.9em; font-weight: bold; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; overflow: hidden; border-radius: 8px; }}
        th, td {{ border: 1px solid #e0e0e0; padding: 10px 15px; text-align: right; }}
        th {{ background-color: #42a5f5; color: white; font-weight: normal; font-size: 1em; position: sticky; top: 0; }}
        th:first-child, td:first-child {{ text-align: left; }}
        tr:nth-child(even) {{ background-color: #f5f5f5; }}
        tr:hover {{ background-color: #e0f2f1; }}
        .download-btn {{ background-color: #4caf50; color: white; padding: 10px 15px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; font-weight: bold; display: inline-block; transition: background-color 0.3s; }}
        .download-btn:hover {{ background-color: #388e3c; }}
        .footer {{ text-align: center; color: #999; margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; font-size: 0.8em; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📈 Stock Financial Dashboard</h1>
        <p>Real-time stock data from NSE India Official API</p>
        <div class="header-row">
            <a href="/download_csv" class="download-btn">📥 Download Raw Data (.csv)</a>
            <div class="info-box">
                🕐 <strong>Last Updated:</strong> {last_updated_str} | Status: {status_msg}
            </div>
        </div>
        <h2>📊 Stock Price Data - All Symbols</h2>
        <div style="max-height: 70vh; overflow-y: auto;">
            <table>
                {table_content_html}
            </table>
        </div>
        <div class="footer">
            <p>Data provided by NSE India | Dashboard built with Python Flask & APScheduler</p>
        </div>
    </div>
</body>
</html>
"""
    return html_content

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
