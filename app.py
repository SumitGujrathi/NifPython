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

# Mapping your dashboard names directly to NSE Symbols
STOCK_LIST = [
    "NIFTY_50", "NIFTY_BANK", "ACC", "ADANIPORTS", "SBIN", "AMBUJACEM",
    "WIPRO", "APOLLOTYRE", "ASIANPAINT", "AUROPHARMA", "AXISBANK",
    "BAJFINANCE", "IOC", "BANKBARODA", "BATAINDIA", "BERGEPAINT", "BHARATFORG",
    "COALINDIA", "INDUSINDBK", "DRREDDY", "INFY", "JSWSTEEL", "POWERGRID",
    "LICHSGFIN", "CANBK", "MGL", "M&MFIN", "HDFCBANK", "MANAPPURAM", "MARICO",
    "SUNTV", "HINDZINC", "ICICIBANK", "ZEEL"
]

NSE_SYMBOL_MAP = {
    "NIFTY_50": "NIFTY 50",
    "NIFTY_BANK": "NIFTY BANK",
    "M&MFIN": "M&MFIN"
}

DATA_CACHE = {"data": pd.DataFrame(), "timestamp": None, "status": "Initializing..."}
CACHE_LOCK = threading.Lock()

def fetch_nse_stock_quote(symbol):
    """Fetches real-time price directly from official NSE India endpoint."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/get-quotes/equity?symbol=SBIN"
    }
    
    session = curl_requests.Session(impersonate="chrome")
    
    # NSE requires visiting the homepage first to set cookies
    try:
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
    except Exception:
        pass

    nse_symbol = NSE_SYMBOL_MAP.get(symbol, symbol)
    
    # Endpoint routing for Indices vs Equities
    if "NIFTY" in nse_symbol:
        url = f"https://www.nseindia.com/api/allIndices"
    else:
        url = f"https://www.nseindia.com/api/quote-equity?symbol={nse_symbol}"

    try:
        resp = session.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            json_data = resp.json()
            
            if "NIFTY" in nse_symbol:
                for idx in json_data.get('data', []):
                    if idx.get('index') == nse_symbol:
                        return {
                            "Symbol": symbol,
                            "Current Price / LTP": idx.get('last'),
                            "Open": idx.get('open'),
                            "High": idx.get('high'),
                            "Low": idx.get('low'),
                            "Previous Close": idx.get('previousClose'),
                            "Volume": None
                        }
            else:
                price_info = json_data.get('priceInfo', {})
                sec_detail = json_data.get('securityInfo', {})
                return {
                    "Symbol": symbol,
                    "Current Price / LTP": price_info.get('lastPrice'),
                    "Open": price_info.get('open'),
                    "High": price_info.get('intraDayHighLow', {}).get('max'),
                    "Low": price_info.get('intraDayHighLow', {}).get('min'),
                    "Previous Close": price_info.get('previousClose'),
                    "Volume": json_data.get('preOpenMarket', {}).get('totalTradedVolume')
                }
    except Exception as e:
        logging.warning(f"NSE Fetch failed for {symbol}: {e}")

    return None

def fetch_all_nse_data():
    all_rows = []
    for symbol in STOCK_LIST:
        data = fetch_nse_stock_quote(symbol)
        if data:
            all_rows.append(data)
        else:
            all_rows.append({
                "Symbol": symbol, "Current Price / LTP": None, "Open": None,
                "High": None, "Low": None, "Previous Close": None, "Volume": None
            })
        time.sleep(0.2) # Friendly delay to avoid rate limiting
    return pd.DataFrame(all_rows)

def background_update_job():
    logging.info("Triggering NSE Live Data Refresh...")
    df_new = fetch_all_nse_data()

    with CACHE_LOCK:
        if not df_new.empty and df_new['Current Price / LTP'].notna().any():
            DATA_CACHE["data"] = df_new
            DATA_CACHE["timestamp"] = datetime.now()
            DATA_CACHE["status"] = "Active (NSE India Official)"
            logging.info("Cache successfully updated with NSE India data.")
        else:
            DATA_CACHE["status"] = "Retrying NSE in background..."

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(background_update_job, 'interval', seconds=60)
scheduler.start()

threading.Thread(target=background_update_job, daemon=True).start()

app = Flask(__name__)

def format_dataframe_to_html_table(df):
    if df.empty:
        return '<tr><td colspan="7" style="text-align: center; color: #1976d2; font-weight: bold;">Loading live data from NSE India... Please refresh in a few seconds.</td></tr>', []

    formatted_df = df.copy()
    numeric_cols = formatted_df.columns.drop('Symbol')
    formatted_df = formatted_df.dropna(subset=numeric_cols, how='all').reset_index(drop=True)

    for col in ["Current Price / LTP", "Open", "High", "Low", "Previous Close"]:
        numeric_col = pd.to_numeric(formatted_df[col], errors='coerce')
        formatted_df[col] = numeric_col.apply(lambda x: "N/A" if pd.isna(x) or x == 0 else f"₹{x:,.2f}")

    numeric_volume = pd.to_numeric(formatted_df["Volume"], errors='coerce')
    formatted_df["Volume"] = numeric_volume.apply(lambda x: "N/A" if pd.isna(x) or x == 0 else f"{x:,.0f}")

    headers = formatted_df.columns.tolist()
    header_html = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    rows_html = "".join(f"<tr>" + "".join(f"<td>{row[col]}</td>" for col in headers) + "</tr>" for _, row in formatted_df.iterrows())

    return f"<thead>{header_html}</thead><tbody>{rows_html}</tbody>", headers

@app.route("/")
def dashboard():
    with CACHE_LOCK:
        df_data = DATA_CACHE["data"].copy()
        last_updated_dt = DATA_CACHE["timestamp"]
        status_msg = DATA_CACHE["status"]

    table_content_html, _ = format_dataframe_to_html_table(df_data)
    last_updated_str = last_updated_dt.strftime("%Y-%m-%d %H:%M:%S") if last_updated_dt else "Loading..."

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="60">
    <title>📈 Live Stock Financial Dashboard</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, sans-serif; margin: 0; padding: 20px; background-color: #f0f2f6; }}
        .container {{ max-width: 1400px; margin: auto; background-color: white; padding: 30px; border-radius: 12px; box-shadow: 0 6px 15px rgba(0,0,0,0.1); }}
        h1 {{ color: #1e88e5; text-align: center; }}
        .info-box {{ background-color: #e3f2fd; color: #0d47a1; padding: 10px; border-radius: 6px; font-weight: bold; float: right; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        th, td {{ border: 1px solid #e0e0e0; padding: 10px; text-align: right; }}
        th {{ background-color: #42a5f5; color: white; }}
        th:first-child, td:first-child {{ text-align: left; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📈 Stock Financial Dashboard</h1>
        <div class="info-box">🕐 Last Updated: {last_updated_str} | Status: {status_msg}</div>
        <div style="clear: both;"></div>
        <div style="max-height: 70vh; overflow-y: auto;">
            <table>{table_content_html}</table>
        </div>
    </div>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
