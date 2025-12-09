from flask import Flask, jsonify
import requests
import os
import pandas as pd
import numpy as np # Keep this for consistent NaN handling

app = Flask(__name__)

# --- Configuration ---
# NSE API Tickers use the base symbol (e.g., INFY, not INFY.NS)
STOCK_TICKERS = [
    'INFY',
    'TCS',
    'RELIANCE',
    'HDFCBANK'
]

# Base URL for the NSE API
NSE_BASE_URL = 'https://www.nseindia.com/'
NSE_QUOTE_URL = 'https://www.nseindia.com/api/quote-equity?symbol='

# Headers required to mimic a browser and maintain a session
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive'
}


def get_nse_session_cookies():
    """
    Establishes a session by hitting the base URL to get necessary cookies.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    
    # Hit the base URL once to get initial cookies (crucial for authentication)
    session.get(NSE_BASE_URL, timeout=10)
    return session


def fetch_stock_data_raw(ticker_list):
    table_headers = ["Symbol", "LTP (Close)", "Open", "High", "Low", "P. Close", "Volume"]
    table_data = []

    try:
        # Start a new session for all requests
        session = get_nse_session_cookies()
    except Exception as e:
        return {"error": f"Failed to establish NSE session: {e}"}

    for ticker in ticker_list:
        url = NSE_QUOTE_URL + ticker
        
        try:
            # 1. Fetch data for the current ticker using the established session
            response = session.get(url, timeout=10)
            response.raise_for_status() # Raise exception for bad status codes (4xx or 5xx)
            
            data = response.json()
            
            # 2. Extract specific data points
            if not data.get('priceInfo'):
                table_data.append([ticker, 'No Price Data', 0, 0, 0, 0, 0])
                continue

            price_info = data['priceInfo']
            market_info = data['marketStatus']
            
            # Handle possible market closures/errors
            if market_info.get('marketStatus') != 'Open' and market_info.get('marketStatus') != 'Closed':
                 current_price = price_info.get('close') or price_info.get('lastPrice')
            else:
                 current_price = price_info.get('lastPrice')
            
            # The previous close is found under securityInfo
            previous_close = data['securityInfo']['prevClose']

            # 3. Format the row data
            row = [
                ticker,
                price_info.get('lastPrice', 0.0), # LTP
                price_info.get('open', 0.0),      # Open
                price_info.get('dayHigh', 0.0),   # High
                price_info.get('dayLow', 0.0),    # Low
                previous_close,                   # P. Close
                data['totalTradedVolume']         # Volume
            ]
            
            # Convert values to correct types for Apps Script
            # (Ensuring numbers are floats/integers, not strings)
            row = [row[0]] + [float(x) if isinstance(x, (int, float, str)) and str(x).replace('.', '', 1).isdigit() else 0.0 for x in row[1:6]] + [int(row[6]) if str(row[6]).isdigit() else 0]
            
            table_data.append(row)

        except requests.exceptions.HTTPError as e:
            # This handles 403 Forbidden errors if the NSE API blocks the IP
            table_data.append([ticker, f"HTTP Error: {e.response.status_code}", 0, 0, 0, 0, 0])
        except Exception as e:
            table_data.append([ticker, f"Processing Error: {str(e)}", 0, 0, 0, 0, 0])


    if not table_data:
        return {"error": "NSE returned empty data or was blocked for all tickers."}
        
    return {"headers": table_headers, "data": table_data}

# --- Flask and Startup Routes remain the same ---

@app.route('/')
def home():
    return "Stock Data API is running!", 200

@app.route('/get-live-data')
def get_live_data():
    data = fetch_stock_data_raw(STOCK_TICKERS)
    if 'error' in data:
        return jsonify(data), 500  
    return jsonify(data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
