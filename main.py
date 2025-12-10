import streamlit as st
from streamlit_autorefresh import st_autorefresh
import yfinance as yf
import pandas as pd
import numpy as np # Import numpy for NaN handling
from datetime import datetime

# --- CONFIGURATION ---
st.set_page_config(page_title="Stock Financial Dashboard",
                   page_icon="📈",
                   layout="wide")

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


@st.cache_data(ttl=60)
def get_all_stocks_data():
    """Fetches stock data and returns a DataFrame with numeric data types.
       Uses None for missing values instead of "N/A" to avoid pyarrow errors."""
    
    all_data = []

    yahoo_symbols = [get_yahoo_symbol(stock) for stock in STOCK_LIST]
    symbols_str = " ".join(yahoo_symbols)

    # Use None for default values, which Pandas treats as NaN (numeric missing data)
    DEFAULT_VALUE = None 

    try:
        # First attempt: Fetch all in one go using yf.Tickers
        tickers = yf.Tickers(symbols_str)

        for stock_name in STOCK_LIST:
            yahoo_symbol = get_yahoo_symbol(stock_name)
            try:
                ticker = tickers.tickers.get(yahoo_symbol)
                if ticker:
                    info = ticker.info

                    # Use .get(key, DEFAULT_VALUE) to ensure a numeric type or None
                    current_price = info.get('currentPrice') or info.get(
                        'regularMarketPrice', DEFAULT_VALUE)
                    open_price = info.get('open') or info.get(
                        'regularMarketOpen', DEFAULT_VALUE)
                    high_price = info.get('dayHigh') or info.get(
                        'regularMarketDayHigh', DEFAULT_VALUE)
                    low_price = info.get('dayLow') or info.get(
                        'regularMarketDayLow', DEFAULT_VALUE)
                    prev_close = info.get('previousClose') or info.get(
                        'regularMarketPreviousClose', DEFAULT_VALUE)
                    volume = info.get('volume') or info.get(
                        'regularMarketVolume', DEFAULT_VALUE)

                    all_data.append({
                        "Symbol": stock_name,
                        # Use collected value (numeric or None)
                        "Current Price / LTP": current_price,
                        "Open": open_price,
                        "High": high_price,
                        "Low": low_price,
                        "Previous Close": prev_close,
                        "Volume": volume
                    })
                else:
                    # Ticker object not found
                    all_data.append({
                        "Symbol": stock_name,
                        "Current Price / LTP": DEFAULT_VALUE,
                        "Open": DEFAULT_VALUE,
                        "High": DEFAULT_VALUE,
                        "Low": DEFAULT_VALUE,
                        "Previous Close": DEFAULT_VALUE,
                        "Volume": DEFAULT_VALUE
                    })
            except Exception:
                # Error fetching single ticker info
                all_data.append({
                    "Symbol": stock_name,
                    "Current Price / LTP": DEFAULT_VALUE,
                    "Open": DEFAULT_VALUE,
                    "High": DEFAULT_VALUE,
                    "Low": DEFAULT_VALUE,
                    "Previous Close": DEFAULT_VALUE,
                    "Volume": DEFAULT_VALUE
                })
    except Exception:
        # Second attempt: If yf.Tickers (batch fetch) fails, try individual yf.Ticker
        for stock_name in STOCK_LIST:
            yahoo_symbol = get_yahoo_symbol(stock_name)
            try:
                ticker = yf.Ticker(yahoo_symbol)
                info = ticker.info

                current_price = info.get('currentPrice') or info.get(
                    'regularMarketPrice', DEFAULT_VALUE)
                open_price = info.get('open') or info.get(
                    'regularMarketOpen', DEFAULT_VALUE)
                high_price = info.get('dayHigh') or info.get(
                    'regularMarketDayHigh', DEFAULT_VALUE)
                low_price = info.get('dayLow') or info.get(
                    'regularMarketDayLow', DEFAULT_VALUE)
                prev_close = info.get('previousClose') or info.get(
                    'regularMarketPreviousClose', DEFAULT_VALUE)
                volume = info.get('volume') or info.get(
                    'regularMarketVolume', DEFAULT_VALUE)

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
                    "Current Price / LTP": DEFAULT_VALUE,
                    "Open": DEFAULT_VALUE,
                    "High": DEFAULT_VALUE,
                    "Low": DEFAULT_VALUE,
                    "Previous Close": DEFAULT_VALUE,
                    "Volume": DEFAULT_VALUE
                })

    # Return DataFrame where numeric columns contain only numbers or NaN/None
    return pd.DataFrame(all_data)


def format_dataframe(df):
    """Formats the DataFrame for display, converting NaN/None values to "N/A"."""
    formatted_df = df.copy()

    # Coerce to numeric, errors='coerce' turns non-numeric (if any slipped) into NaN
    # Fill NaN with 0 for consistent formatting logic
    
    # 1. Format Price Columns
    for col in [
            "Current Price / LTP", "Open", "High", "Low", "Previous Close"
    ]:
        # Convert to float, coercing any non-numeric values (like old 'N/A') to NaN
        numeric_col = pd.to_numeric(formatted_df[col], errors='coerce')
        
        # Apply formatting: check for NaN or 0, otherwise format as currency
        formatted_df[col] = numeric_col.apply(
            lambda x: "N/A" if pd.isna(x) or x == 0 else f"₹{x:,.2f}")

    # 2. Format Volume Column
    
    # Convert to numeric, coercing any non-numeric values to NaN
    numeric_volume = pd.to_numeric(formatted_df["Volume"], errors='coerce')
    
    # Apply formatting: check for NaN or 0, otherwise format as whole number volume
    formatted_df["Volume"] = numeric_volume.apply(
        lambda x: "N/A" if pd.isna(x) or x == 0 else f"{x:,.0f}")

    return formatted_df


def main():
    # Auto-refresh every 60000 milliseconds (1 minute)
    st_autorefresh(interval=60000, limit=None, key="stock_refresh")

    st.title("📈 Stock Financial Dashboard")
    st.markdown(
        "Real-time stock data from Yahoo Finance | Auto-refreshes every 1 minute"
    )

    col1, col2 = st.columns([3, 1])
    with col2:
        last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.info(f"🕐 Last Updated: {last_updated}")

    st.markdown("---")

    with st.spinner("Fetching stock data for all symbols..."):
        # The DataFrame returned here is numeric (except Symbol) and safe for caching
        df = get_all_stocks_data()

    # Filter out rows where all numeric data is missing (i.e., failed fetch)
    numeric_cols = df.columns.drop('Symbol')
    df_clean = df.dropna(subset=numeric_cols, how='all')


    if not df_clean.empty:
        st.subheader("📊 Stock Price Data - All Symbols")

        # Format the numeric DataFrame for display
        formatted_df = format_dataframe(df_clean)

        st.dataframe(formatted_df, height=800, hide_index=True)

        st.markdown("---")

        # Use the original numeric DataFrame for CSV download for better data integrity
        csv_data = df_clean.to_csv(index=False)
        st.download_button(
            label="📥 Download Data as CSV",
            data=csv_data,
            file_name="stock_data.csv",
            mime="text/csv",
            key="static_download_link"
        )
    else:
        st.warning("Unable to fetch any stock data. Please check connection and stock symbols.")

    st.markdown("---")
    st.markdown("""
        <div style='text-align: center; color: #666;'>
            <p>Data provided by Yahoo Finance | Auto-refreshes every 60 seconds</p>
        </div>
        """,
                unsafe_allow_html=True)


if __name__ == "__main__":
    main()
