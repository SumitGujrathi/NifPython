from flask import Flask, jsonify
import yfinance as yf
import pandas as pd
import numpy as np

app = Flask(__name__)

# --- Your Stock Tickers List ---
STOCK_TICKERS = [
    'INFY.NS',
    'TCS.NS',
    'RELIANCE.NS',
    'HDFCBANK.NS'
]

def fetch_stock_data_raw(ticker_list):
    """
    Fetches raw stock data using yfinance and returns it as a list of lists 
    (ready for Apps Script).
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
                latest_data = ticker_data.iloc[-1]
                
                previous_close = ticker_data['Close'].iloc[-2] if len(ticker_data) >= 2 else None

                # Format volume gracefully
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
            table_data.append([ticker, f"Error: {e}", 0, 0, 0, 0, 0])
            continue

    return {"headers": table_headers, "data": table_data}

@app.route('/get-live-data')
def get_live_data():
    """API endpoint to fetch and return the stock data as JSON."""
    data = fetch_stock_data_raw(STOCK_TICKERS)
    
    if 'error' in data:
        return jsonify(data), 500  # Return error with HTTP 500 status
        
    return jsonify(data)

if __name__ == '__main__':
    # When running locally
    app.run(debug=True)
