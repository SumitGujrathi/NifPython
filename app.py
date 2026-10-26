import os
import time
import logging
import threading
from datetime import datetime
import pandas as pd
from flask import Flask, Response
from apscheduler.schedulers.background import BackgroundScheduler
import upstox_client
from upstox_client.rest import ApiException

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Upstox API Configuration
UPSTOX_API_KEY = os.environ.get("UPSTOX_API_KEY", "YOUR_UPSTOX_API_KEY")
UPSTOX_ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")

# Upstox Instrument Keys for your tickers
UPSTOX_INSTRUMENTS = {
    "NIFTY_50": "NSE_INDEX|Nifty 50",
    "NIFTY_BANK": "NSE_INDEX|Nifty Bank",
    "SBIN": "NSE_EQ|INE062A01020",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "INFY": "NSE_EQ|INE009A01021",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "AXISBANK": "NSE_EQ|INE238A01034",
    "WIPRO": "NSE_EQ|INE075A01022",
    "ASIANPAINT": "ASIANPAINT",
    "ADANIPORTS": "NSE_EQ|INE742F01042"
}

STOCK_LIST = list(UPSTOX_INSTRUMENTS.keys())

DATA_CACHE = {"data": pd.DataFrame(), "timestamp": None, "status": "Initializing..."}
CACHE_LOCK = threading.Lock()

def fetch_upstox_market_quote():
    """Fetches clean real-time quotes directly from Upstox official servers."""
    if not UPSTOX_ACCESS_TOKEN or UPSTOX_ACCESS_TOKEN == "YOUR_ACCESS_TOKEN":
        logging.warning("Upstox Access Token missing. Set UPSTOX_ACCESS_TOKEN environment variable.")
        return pd.DataFrame()

    configuration = upstox_client.Configuration()
    configuration.access_token = UPSTOX_ACCESS_TOKEN
    api_instance = upstox_client.MarketQuoteApi(upstox_client.ApiClient(configuration))

    instrument_keys = ",".join(UPSTOX_INSTRUMENTS.values())

    try:
        api_response = api_instance.get_full_market_quote(instrument_keys, "2.0")
        quotes = api_response.data
        
        all_rows = []
        for stock_name, key in UPSTOX_INSTRUMENTS.items():
            # Match returned instrument key
            formatted_key = key.replace("|", ":")
            quote_data = quotes.get(formatted_key) or quotes.get(key)
            
            if quote_data:
                ohlc = quote_data.ohlc or {}
                all_rows.append({
                    "Symbol": stock_name,
                    "Current Price / LTP": quote_data.last_price,
                    "Open": ohlc.open,
                    "High": ohlc.high,
                    "Low": ohlc.low,
                    "Previous Close": ohlc.close,
                    "Volume": quote_data.volume
                })
            else:
                all_rows.append({
                    "Symbol": stock_name, "Current Price / LTP": None, "Open": None,
                    "High": None, "Low": None, "Previous Close": None, "Volume": None
                })
        return pd.DataFrame(all_rows)

    except ApiException as e:
        logging.error(f"Upstox API Exception: {e}")
    except Exception as e:
        logging.error(f"Error fetching Upstox data: {e}")

    return pd.DataFrame()

def background_update_job():
    logging.info("Fetching market data via Upstox API...")
    df_new = fetch_upstox_market_quote()

    with CACHE_LOCK:
        if not df_new.empty and df_new['Current Price / LTP'].notna().any():
            DATA_CACHE["data"] = df_new
            DATA_CACHE["timestamp"] = datetime.now()
            DATA_CACHE["status"] = "Active (Upstox Official)"
            logging.info("Cache updated successfully.")
        else:
            DATA_CACHE["status"] = "Waiting for valid API token..."

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(background_update_job, 'interval', seconds=60)
scheduler.start()

threading.Thread(target=background_update_job, daemon=True).start()

app = Flask(__name__)

def format_dataframe_to_html_table(df):
    if df.empty:
        return '<tr><td colspan="7" style="text-align: center; color: #1976d2; font-weight: bold;">Connecting to Upstox API... Please verify your Access Token.</td></tr>', []

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
        <h1>📈 Live Stock Financial Dashboard</h1>
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
