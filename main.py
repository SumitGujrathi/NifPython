from flask import Flask, jsonify
import yfinance as yf
import pandas as pd
import numpy as np
import os
import json # Used for error logging/handling

app = Flask(__name__)

# --- Configuration ---
# Use .NS suffix for NSE tickers (e.g., INFY.NS)
STOCK_TICKERS = [
    'INFY.NS',
    'TCS.NS',
    'RELIANCE.NS',
    'HDFCBANK.NS'
]

def fetch_stock_data_raw(ticker_list):
    """
    Fetches raw stock data using yfinance and returns it as a dictionary 
    with headers and data rows, ready for Apps Script.
    """
    table_headers = ["Symbol", "LTP (Close)", "Open", "High", "Low", "P. Close", "Volume"]
    table_data = []
    
    try:
        data = yf.download(
            tickers=ticker_list,
            period="2d",
            interval="1d",
            progress=False,
            group_by='ticker'
        )
    except Exception as e:
        return {"error": f"YFinance Download Error: {e}"}

    if data.empty:
        return {"error": "YFinance returned empty data."}

    for ticker in ticker_list:
        try:
            if ticker in data.columns.get_level_values(0):
                ticker_data = data[ticker]
                
                # Check for empty data for the specific ticker
                if ticker_data.empty or ticker_data.iloc[-1].isnull().all():
                     table_data.append([ticker, 'N/A', 0, 0, 0, 0, 0])
                     continue
                
                latest_data = ticker_data.iloc[-1]
                
                # Get Previous Close (The 'Close' from the second-to-last row)
                previous_close = ticker_data['Close'].iloc[-2] if len(ticker_data) >= 2 else None

                # Safely get and format volume
                volume_value = latest_data['Volume']
                volume = int(volume_value) if not pd.isna(volume_value) and volume_value != 0 else 0

                # Function to safely handle price values (float or 0)
                def get_price(value):
                    return float(value) if not pd.isna(value) else 0.0

                # Append the raw numeric/string data row
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
            # If a specific ticker fails, append an error row
            table_data.append([ticker, f"Processing Error: {str(e)}", 0, 0, 0, 0, 0])
            continue

    return {"headers": table_headers, "data": table_data}

@app.route('/get-live-data')
def get_live_data():
    """API endpoint to fetch and return the stock data as JSON."""
    data = fetch_stock_data_raw(STOCK_TICKERS)
    
    if 'error' in data:
        # Render needs a JSON response even for errors
        return jsonify(data), 500  
        
    return jsonify(data)

# --- Crucial for Render Deployment ---
# Use Gunicorn with the PORT environment variable provided by Render
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
