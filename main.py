from flask import Flask, jsonify
import yfinance as yf
import pandas as pd
import numpy as np
import os
# REMOVED: import requests, HTTPAdapter, Retry

app = Flask(__name__)

# --- Configuration ---
STOCK_TICKERS = [
    'INFY.NS',
    'TCS.NS',
    'RELIANCE.NS',
    'HDFCBANK.NS'
]

# REMOVED: get_retry_session function

@app.route('/')
def home():
    return "Stock Data API is running!", 200

def fetch_stock_data_raw(ticker_list):
    table_headers = ["Symbol", "LTP (Close)", "Open", "High", "Low", "P. Close", "Volume"]
    table_data = []
    
    # REMOVED: session = get_retry_session()

    try:
        # NOTE: session=session argument IS REMOVED here
        data = yf.download(
            tickers=ticker_list,
            period="5d",
            interval="1d",
            progress=False,
            group_by='ticker',
            # REMOVED: session=session 
        )
    except Exception as e:
        return {"error": f"YFinance Download Error: {e}"}

    if data.empty:
        return {"error": "YFinance returned empty data."}

    # ... (Rest of your data processing logic remains the same) ...
    for ticker in ticker_list:
        try:
            if ticker in data.columns.get_level_values(0):
                ticker_data = data[ticker]
                
                if ticker_data.empty or ticker_data.iloc[-1].isnull().all():
                     table_data.append([ticker, 'N/A', 0, 0, 0, 0, 0])
                     continue
                
                latest_data = ticker_data.iloc[-1]
                previous_close = ticker_data['Close'].iloc[-2] if len(ticker_data) >= 2 else None

                volume_value = latest_data['Volume']
                volume = int(volume_value) if not pd.isna(volume_value) and volume_value != 0 else 0

                def get_price(value):
                    return float(value) if not pd.isna(value) else 0.0

                row = [
                    ticker,
                    get_price(latest_data['Close']),
                    get_price(latest_data['Open']),
                    get_price(latest_data['High']),
                    get_price(latest_data['Low']),
                    get_price(previous_close),
                    volume
                ]
                table_data.append(row)
        
        except Exception as e:
            table_data.append([ticker, f"Processing Error: {str(e)}", 0, 0, 0, 0, 0])
            continue

    return {"headers": table_headers, "data": table_data}

@app.route('/get-live-data')
def get_live_data():
    data = fetch_stock_data_raw(STOCK_TICKERS)
    if 'error' in data:
        return jsonify(data), 500  
    return jsonify(data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
