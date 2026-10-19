# app.py

import yfinance as yf
import pandas as pd
from datetime import datetime
from flask import Flask, Response

# --- CONFIGURATION (Stock Lists) ---
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


def get_yahoo_symbol(stock_name):
    return SYMBOL_MAPPING.get(stock_name, f"{stock_name}.NS")

# --- FLASK CACHING IMPLEMENTATION ---
CACHE = {'data': None, 'timestamp': datetime.min}
CACHE_TTL_SECONDS = 60 # Time To Live (1 minute)

def get_all_stocks_data_cached():
    """Checks cache and fetches new data if cache is expired."""
    global CACHE
    
    # Check if cache is fresh
    if CACHE['data'] is not None and \
       (datetime.now() - CACHE['timestamp']).total_seconds() < CACHE_TTL_SECONDS:
        return CACHE['data'].copy(), CACHE['timestamp'] 

    # Fetch new data
    df = _fetch_stocks_data_yfinance()
    current_time = datetime.now()
    
    # Update cache
    CACHE['data'] = df
    CACHE['timestamp'] = current_time
    
    return df.copy(), current_time


def _fetch_stocks_data_yfinance():
    """Core function to fetch stock data using yfinance."""
    
    all_data = []
    yahoo_symbols = [get_yahoo_symbol(stock) for stock in STOCK_LIST]
    symbols_str = " ".join(yahoo_symbols)
    DEFAULT_VALUE = None 

    try:
        # Attempt 1: Batch fetch using yf.Tickers
        tickers = yf.Tickers(symbols_str)

        for stock_name in STOCK_LIST:
            yahoo_symbol = get_yahoo_symbol(stock_name)
            try:
                ticker = tickers.tickers.get(yahoo_symbol)
                info = ticker.info if ticker else {}

                # Fallback logic for price and volume data
                current_price = info.get('currentPrice') or info.get('regularMarketPrice', DEFAULT_VALUE)
                open_price = info.get('open') or info.get('regularMarketOpen', DEFAULT_VALUE)
                high_price = info.get('dayHigh') or info.get('regularMarketDayHigh', DEFAULT_VALUE)
                low_price = info.get('dayLow') or info.get('regularMarketDayLow', DEFAULT_VALUE)
                prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose', DEFAULT_VALUE)
                volume = info.get('volume') or info.get('regularMarketVolume', DEFAULT_VALUE)

                all_data.append({
                    "Symbol": stock_name,
                    "Current Price / LTP": current_price,
                    "Open": open_price,
                    "High": high_price,
                    "Low": low_price,
                    "Previous Close": prev_close,
                    "Volume": volume
                })
            except Exception:
                # Error fetching single ticker info
                all_data.append({
                    "Symbol": stock_name,
                    "Current Price / LTP": DEFAULT_VALUE, "Open": DEFAULT_VALUE, 
                    "High": DEFAULT_VALUE, "Low": DEFAULT_VALUE, 
                    "Previous Close": DEFAULT_VALUE, "Volume": DEFAULT_VALUE
                })
    except Exception:
        # If batch fetch fails entirely, return whatever data was collected (if any)
        pass


    # Return DataFrame
    return pd.DataFrame(all_data)


def format_dataframe_to_html_table(df):
    """
    Formats the raw DataFrame into an HTML string for display.
    """
    formatted_df = df.copy()

    # 1. Filter out rows where all numeric data is missing 
    numeric_cols = formatted_df.columns.drop('Symbol')
    formatted_df = formatted_df.dropna(subset=numeric_cols, how='all').reset_index(drop=True)

    if formatted_df.empty:
        return '<tr><td colspan="7" style="text-align: center; color: red;">No stock data available to display.</td></tr>', []

    # 2. Format Price Columns
    for col in [
            "Current Price / LTP", "Open", "High", "Low", "Previous Close"
    ]:
        numeric_col = pd.to_numeric(formatted_df[col], errors='coerce')
        formatted_df[col] = numeric_col.apply(
            lambda x: "N/A" if pd.isna(x) or x == 0 else f"₹{x:,.2f}")

    # 3. Format Volume Column
    numeric_volume = pd.to_numeric(formatted_df["Volume"], errors='coerce')
    formatted_df["Volume"] = numeric_volume.apply(
        lambda x: "N/A" if pd.isna(x) or x == 0 else f"{x:,.0f}")
    
    # 4. Generate HTML Table Headers and Rows
    headers = formatted_df.columns.tolist()
    
    header_html = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    
    rows_html = ""
    for _, row in formatted_df.iterrows():
        row_cells = "".join(f"<td>{row[col]}</td>" for col in headers)
        rows_html += f"<tr>{row_cells}</tr>"
        
    table_html = f"<thead>{header_html}</thead><tbody>{rows_html}</tbody>"

    return table_html, headers


# --- FLASK SETUP ---
app = Flask(__name__)

@app.route("/download_csv")
def download_csv():
    """Route to download the raw stock data as a CSV file."""
    
    df, _ = get_all_stocks_data_cached()
    numeric_cols = df.columns.drop('Symbol')
    df_clean = df.dropna(subset=numeric_cols, how='all')

    if df_clean.empty:
        return "Error: No data available for download.", 404

    csv_data = df_clean.to_csv(index=False)
    
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-disposition":
                 "attachment; filename=stock_data_live.csv"}
    )

@app.route("/")
def dashboard():
    """The main route that generates the entire HTML page."""
    
    # Get cached data and timestamp
    df_data, last_updated_dt = get_all_stocks_data_cached()
    
    # Generate the HTML table content and get headers
    table_content_html, _ = format_dataframe_to_html_table(df_data)

    last_updated_str = last_updated_dt.strftime("%Y-%m-%d %H:%M:%S")

    # --- Full HTML String Generation ---
    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="{CACHE_TTL_SECONDS}">
    <title>📈 Stock Financial Dashboard (Single File Flask)</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f0f2f6; 
            color: #333;
        }}
        .container {{
            max-width: 1400px;
            margin: auto;
            background-color: white;
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 6px 15px rgba(0, 0, 0, 0.1);
        }}
        h1 {{
            color: #1e88e5;
            border-bottom: 3px solid #eee;
            padding-bottom: 10px;
            text-align: center;
        }}
        .header-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        .info-box {{
            background-color: #e3f2fd;
            color: #0d47a1;
            padding: 10px 15px;
            border-radius: 6px;
            font-size: 0.9em;
            font-weight: bold;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
            overflow: hidden; 
            border-radius: 8px;
        }}
        th, td {{
            border: 1px solid #e0e0e0;
            padding: 10px 15px;
            text-align: right;
        }}
        th {{
            background-color: #42a5f5;
            color: white;
            font-weight: normal;
            font-size: 1em;
            position: sticky;
            top: 0;
        }}
        th:first-child, td:first-child {{
            text-align: left;
        }}
        tr:nth-child(even) {{
            background-color: #f5f5f5;
        }}
        tr:hover {{
            background-color: #e0f2f1;
        }}
        .download-btn {{
            background-color: #4caf50;
            color: white;
            padding: 10px 15px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            text-decoration: none;
            font-weight: bold;
            display: inline-block;
            transition: background-color 0.3s;
        }}
        .download-btn:hover {{
            background-color: #388e3c;
        }}
        .footer {{
            text-align: center;
            color: #999;
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            font-size: 0.8em;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📈 Stock Financial Dashboard</h1>
        <p>Real-time stock data from Yahoo Finance</p>
        
        <div class="header-row">
            <a href="/download_csv" class="download-btn">📥 Download Raw Data (.csv)</a>
            <div class="info-box">
                🕐 <strong>Last Updated:</strong> {last_updated_str}
            </div>
        </div>

        <h2>📊 Stock Price Data - All Symbols</h2>
        
        <div style="max-height: 70vh; overflow-y: auto;">
            <table>
                {table_content_html}
            </table>
        </div>

        <div class="footer">
            <p>Data provided by Yahoo Finance | Dashboard built with Python Flask (Single Script)</p>
        </div>
    </div>
</body>
</html>
"""
    # Return the generated HTML string
    return html_content


# app.py (Modified end section)

# ... [All your stock data functions and Flask routes] ...

if __name__ == "__main__":
    # --- IMPORTANT ---
    # Render will not use this block; it will use the command in your
    # web service settings (gunicorn app:app).
    # This block is only for local testing.
    # Set debug=False for local testing stability.
    app.run(debug=False, host='0.0.0.0', port=5000)
