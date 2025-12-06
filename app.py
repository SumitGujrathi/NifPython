from flask import Flask, render_template
import pandas as pd
import yfinance as yf
import time
import threading
from datetime import datetime

app = Flask(__name__)

# Global variable to store the fetched data
GLOBAL_STOCK_DATA = []
LAST_UPDATE_TIME = "Never"

# Symbols list (from the previous script)
SYMBOLS_TO_FETCH = [
    '^NSEI', '^NSEBANK', 'ACC.NS', 'ADANIPORTS.NS', 'SBIN.NS', 
    'AMBUJACEM.NS', 'WIPRO.NS', 'APOLLOTYRE.NS', 'ASIANPAINT.NS', 
    'AUROPHARMA.NS', 'AXISBANK.NS', 'BAJFINANCE.NS', 'IOC.NS', 
    'BANKBARODA.NS', 'BATAINDIA.NS', 'BERGEPAINT.NS', 'BHARATFORG.NS', 
    'COALINDIA.NS', 'INDUSINDBK.NS', 'DRREDDY.NS', 'INFY.NS', 
    'JSWSTEEL.NS', 'POWERGRID.NS', 'LICHSGFIN.NS', 'CANBK.NS', 
    'MGL.NS', 'M&MFIN.NS', 'HDFCBANK.NS', 'MANAPPURAM.NS', 
    'MARICO.NS', 'SUNTV.NS', 'HINDZINC.NS', 'ICICIBANK.NS', 'ZEEL.NS'
]

def fetch_data_yf(symbol):
    """Fetches data for a single symbol using yfinance."""
    failed_data = {
        'Symbol': symbol.replace('.NS', '').replace('^NSEI', 'NIFTY 50').replace('^NSEBANK', 'NIFTY BANK'),
        'LTP': 'N/A', 'Open': 'N/A', 'High': 'N/A', 
        'Low': 'N/A', 'Prev. Close': 'N/A', 'Volume': 'N/A'
    }

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        
        if not info:
             raise ValueError("No ticker info available")

        data = {
            'Symbol': symbol.replace('.NS', '').replace('^NSEI', 'NIFTY 50').replace('^NSEBANK', 'NIFTY BANK'),
            'LTP': info.get('currentPrice', info.get('regularMarketPrice', 'N/A')), 
            'Open': info.get('open', 'N/A'),
            'High': info.get('dayHigh', 'N/A'),
            'Low': info.get('dayLow', 'N/A'),
            'Prev. Close': info.get('previousClose', 'N/A'), 
            'Volume': info.get('volume', 'N/A') 
        }
        return data

    except Exception as e:
        print(f"⚠️ Error fetching data for {symbol}: {e}")
        return failed_data

def update_stock_data():
    """Fetches and updates the global stock data."""
    global GLOBAL_STOCK_DATA
    global LAST_UPDATE_TIME
    
    new_data = []
    print(f"\n[Updater] Starting data fetch at {datetime.now().strftime('%H:%M:%S')}")
    
    for symbol in SYMBOLS_TO_FETCH:
        new_data.append(fetch_data_yf(symbol))
        # Reduce delay for faster updates, but be careful not to trigger rate limits
        time.sleep(0.2) 
    
    GLOBAL_STOCK_DATA = new_data
    LAST_UPDATE_TIME = datetime.now().strftime("%H:%M:%S IST")
    print(f"[Updater] Data update complete. Next run in 60 seconds.")


def scheduled_update():
    """Runs the update function periodically."""
    while True:
        update_stock_data()
        # Wait for 60 seconds (1 minute) before the next run
        time.sleep(60) 

# Start the background thread for continuous updates
update_thread = threading.Thread(target=scheduled_update)
update_thread.daemon = True # Allows the main program to exit even if this thread is running
update_thread.start()

# --- Flask Routes ---

@app.route('/')
def index():
    """Render the main index page with the stock data."""
    # Convert list of dicts to DataFrame for easy HTML table generation
    df = pd.DataFrame(GLOBAL_STOCK_DATA)
    # Ensure all numbers are formatted nicely for the web table
    df = df.round(2) 
    
    # Render the HTML template, passing the table and update time
    return render_template(
        'index.html', 
        table_html=df.to_html(classes='table table-striped', index=False, float_format='{:,.2f}'.format),
        last_update=LAST_UPDATE_TIME
    )

if __name__ == '__main__':
    # Initial fetch to populate data before the web server starts
    update_stock_data()
    # Run the Flask app on port 5000
    app.run(debug=True, use_reloader=False)
