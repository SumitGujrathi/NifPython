import os
import time
import logging
import threading
from datetime import datetime
import pandas as pd
from flask import Flask, Response
import yfinance as yf
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from curl_cffi import requests as curl_requests

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# --- CONFIGURATION (Stock Lists & API Keys) ---
ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "YOUR_FREE_ALPHA_VANTAGE_KEY")

STOCK_LIST = [
    "NIFTY_50", "NIFTY_BANK", "ACC", "ADANIPORTS", "SBIN", "AMBUJACEM",
    "WIPRO", "APOLLOTYRE", "ASIANPAINT", "AUROPHARMA", "AXISBANK",
    "BAJFINANCE", "IOC", "BANKBARODA", "BATAINDIA", "BERGEPAINT", "BHARATFORG",
    "COALINDIA", "INDUSINDBK", "DRREDDY", "INFY", "JSWSTEEL", "POWERGRID",
    "LICHSGFIN", "CANBK", "MGL", "M&MFIN", "HDFCBANK", "MANAPPURAM", "MARICO",
    "SUNTV", "HINDZINC", "ICICIBANK", "ZEEL"
]

SYMBOL_MAPPING = {
    "NIFTY_50": "^NSEI",
    "NIFTY_BANK": "^NSEBANK",
    "ACC": "ACC.NS",
    "ADANIPORTS": "ADANIPORTS.NS",
    "SBIN": "SBIN.NS",
    "AMBUJACEM": "AMBUJACEM.NS",
    "WIPRO": "WIPRO.NS",
    "APOLLOTYRE": "APOLLOTYRE.NS",
    "ASIANPAINT": "ASIANPAINT.NS",
    "AUROPHARMA": "AUROPHARMA.NS",
    "AXISBANK": "AXISBANK.NS",
    "BAJFINANCE": "BAJFINANCE.NS",
    "IOC": "IOC.NS",
    "BANKBARODA": "BANKBARODA.NS",
    "BATAINDIA": "BATAINDIA.NS",
    "BERGEPAINT": "BERGEPAINT.NS",
    "BHARATFORG": "BHARATFORG.NS",
    "COALINDIA": "COALINDIA.NS",
    "INDUSINDBK": "INDUSINDBK.NS",
    "DRREDDY": "DRREDDY.NS",
    "INFY": "INFY.NS",
    "JSWSTEEL": "JSWSTEEL.NS",
    "POWERGRID": "POWERGRID.NS",
    "LICHSGFIN": "LICHSGFIN.NS",
    "CANBK": "CANBK.NS",
    "MGL": "MGL.NS",
    "M&MFIN": "M&MFIN.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "MANAPPURAM": "MANAPPURAM.NS",
    "MARICO": "MARICO.NS",
    "SUNTV": "SUNTV.NS",
    "HINDZINC": "HINDZINC.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "ZEEL": "ZEEL.NS"
}

DATA_CACHE = {
    "data": pd.DataFrame(),
    "timestamp": None,
    "status": "Initializing..."
}
CACHE_LOCK = threading.Lock()

def get_yahoo_symbol(stock_name):
    return SYMBOL_MAPPING.get(stock_name, f"{stock_name}.NS")

def get_alpha_vantage_symbol(stock_name):
    # Alpha Vantage uses BSE/NSE suffixes like ACC.BSE or INFY.BSE for Indian equities
    yahoo_sym = get_yahoo_symbol(stock_name)
    if yahoo_sym.endswith(".NS"):
        return yahoo_sym.replace(".NS", ".BSE")
    return yahoo_sym

# --- TIER 2: DIRECT YAHOO API SCRAPER ---
def fetch_direct_yahoo_chart_data(yahoo_symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?range=5d&interval=1d"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = curl_requests.get(url, headers=headers, impersonate="chrome", timeout=8)
        if response.status_code == 200:
            result = response.json()['chart']['result'][0]
            quote = result['indicators']['quote'][0]
            closes = [c for c in quote.get('close', []) if c is not None]
            opens = [o for o in quote.get('open', []) if o is not None]
            highs = [h for h in quote.get('high', []) if h is not None]
            lows = [l for l in quote.get('low', []) if l is not None]
            volumes = [v for v in quote.get('volume', []) if v is not None]

            if closes:
                current_price = closes[-1]
                prev_close = closes[-2] if len(closes) >= 2 else (opens[-1] if opens else current_price)
                return {
                    "Current Price / LTP": current_price,
                    "Open": opens[-1] if opens else None,
                    "High": highs[-1] if highs else None,
                    "Low": lows[-1] if lows else None,
                    "Previous Close": prev_close,
                    "Volume": volumes[-1] if volumes else None
                }
    except Exception as e:
        logging.warning(f"Yahoo Direct API failed for {yahoo_symbol}: {e}")
    return None

# --- TIER 3: ALPHA VANTAGE FALLBACK ---
def fetch_alpha_vantage_quote(stock_name):
    if not ALPHA_VANTAGE_API_KEY or ALPHA_VANTAGE_API_KEY == "YOUR_FREE_ALPHA_VANTAGE_KEY":
        return None

    av_symbol = get_alpha_vantage_symbol(stock_name)
    url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={av_symbol}&apikey={ALPHA_VANTAGE_API_KEY}"
    
    try:
        logging.info(f"Triggering Alpha Vantage Fallback for {av_symbol}...")
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200:
            data = resp.json().get("Global Quote", {})
            if data:
                return {
                    "Current Price / LTP": float(data.get("05. price", 0)),
                    "Open": float(data.get("02. open", 0)),
                    "High": float(data.get("03. high", 0)),
                    "Low": float(data.get("04. low", 0)),
                    "Previous Close": float(data.get("08. previous close", 0)),
                    "Volume": float(data.get("06. volume", 0))
                }
    except Exception as e:
        logging.warning(f"Alpha Vantage API error for {stock_name}: {e}")
    return None

# --- RESILIENT MULTI-TIER FETCH ENGINE ---
def fetch_stock_data_failsafe():
    yahoo_symbols = [get_yahoo_symbol(s) for s in STOCK_LIST]
    df_batch = pd.DataFrame()

    # Tier 1: yfinance Batch Download
    try:
        logging.info("Tier 1: Attempting Batch Download via yfinance...")
        df_batch = yf.download(
            tickers=yahoo_symbols,
            period="5d",
            interval="1d",
            group_by="ticker",
            threads=False,
            progress=False,
            timeout=12
        )
    except Exception as e:
        logging.warning(f"yfinance batch failed: {e}")

    all_data = []

    for stock_name in STOCK_LIST:
        yahoo_symbol = get_yahoo_symbol(stock_name)
        current_price = open_price = high_price = low_price = prev_close = volume = None
        ticker_df = pd.DataFrame()

        # Parse Tier 1 results
        try:
            if not df_batch.empty:
                if isinstance(df_batch.columns, pd.MultiIndex):
                    if yahoo_symbol in df_batch.columns.get_level_values(0):
                        ticker_df = df_batch[yahoo_symbol].dropna(subset=['Close'])
                    elif yahoo_symbol in df_symbol.columns.get_level_values(1):
                        ticker_df = df_batch.xs(yahoo_symbol, level=1, axis=1).dropna(subset=['Close'])
                else:
                    ticker_df = df_batch.dropna(subset=['Close'])
        except Exception:
            pass

        if not ticker_df.empty:
            latest = ticker_df.iloc[-1]
            current_price = float(latest['Close'])
            open_price = float(latest['Open'])
            high_price = float(latest['High'])
            low_price = float(latest['Low'])
            volume = float(latest['Volume']) if 'Volume' in latest and pd.notna(latest['Volume']) else None
            prev_close = float(ticker_df.iloc[-2]['Close']) if len(ticker_df) >= 2 else open_price
        else:
            # Tier 2: Yahoo Direct Chart Endpoint
            direct_data = fetch_direct_yahoo_chart_data(yahoo_symbol)
            if direct_data:
                current_price = direct_data["Current Price / LTP"]
                open_price = direct_data["Open"]
                high_price = direct_data["High"]
                low_price = direct_data["Low"]
                prev_close = direct_data["Previous Close"]
                volume = direct_data["Volume"]
            else:
                # Tier 3: Alpha Vantage Fallback
                av_data = fetch_alpha_vantage_quote(stock_name)
                if av_data:
                    current_price = av_data["Current Price / LTP"]
                    open_price = av_data["Open"]
                    high_price = av_data["High"]
                    low_price = av_data["Low"]
                    prev_close = av_data["Previous Close"]
                    volume = av_data["Volume"]

        all_data.append({
            "Symbol": stock_name,
            "Current Price / LTP": current_price,
            "Open": open_price,
            "High": high_price,
            "Low": low_price,
            "Previous Close": prev_close,
            "Volume": volume
        })

    return pd.DataFrame(all_data)

def background_update_job():
    logging.info("Executing background update...")
    df_new = fetch_stock_data_failsafe()

    with CACHE_LOCK:
        if not df_new.empty and df_new['Current Price / LTP'].notna().any():
            DATA_CACHE["data"] = df_new
            DATA_CACHE["timestamp"] = datetime.now()
            DATA_CACHE["status"] = "Active"
            logging.info("Stock cache refreshed successfully.")
        else:
            DATA_CACHE["status"] = "Retrying in background..."
            logging.warning("Data load returned empty results; preserving existing cache.")

# --- BACKGROUND SCHEDULER SETUP ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(background_update_job, 'interval', seconds=60)
scheduler.start()

threading.Thread(target=background_update_job, daemon=True).start()

# --- FLASK APP ---
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
        <p>Real-time stock data from Multi-Source Financial APIs</p>
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
            <p>Data provided by Yahoo Finance & Alpha Vantage | Dashboard built with Python Flask & APScheduler</p>
        </div>
    </div>
</body>
</html>
"""
    return html_content

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
