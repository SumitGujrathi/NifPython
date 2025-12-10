import streamlit as st
from streamlit_autorefresh import st_autorefresh
import yfinance as yf
import pandas as pd
from datetime import datetime

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
    all_data = []

    yahoo_symbols = [get_yahoo_symbol(stock) for stock in STOCK_LIST]
    symbols_str = " ".join(yahoo_symbols)

    try:
        tickers = yf.Tickers(symbols_str)

        for stock_name in STOCK_LIST:
            yahoo_symbol = get_yahoo_symbol(stock_name)
            try:
                ticker = tickers.tickers.get(yahoo_symbol)
                if ticker:
                    info = ticker.info

                    current_price = info.get('currentPrice') or info.get(
                        'regularMarketPrice') or info.get('previousClose', 0)
                    open_price = info.get('open') or info.get(
                        'regularMarketOpen', 0)
                    high_price = info.get('dayHigh') or info.get(
                        'regularMarketDayHigh', 0)
                    low_price = info.get('dayLow') or info.get(
                        'regularMarketDayLow', 0)
                    prev_close = info.get('previousClose') or info.get(
                        'regularMarketPreviousClose', 0)
                    volume = info.get('volume') or info.get(
                        'regularMarketVolume', 0)

                    all_data.append({
                        "Symbol": stock_name,
                        "Current Price / LTP": current_price,
                        "Open": open_price,
                        "High": high_price,
                        "Low": low_price,
                        "Previous Close": prev_close,
                        "Volume": volume
                    })
                else:
                    all_data.append({
                        "Symbol": stock_name,
                        "Current Price / LTP": "N/A",
                        "Open": "N/A",
                        "High": "N/A",
                        "Low": "N/A",
                        "Previous Close": "N/A",
                        "Volume": "N/A"
                    })
            except Exception:
                all_data.append({
                    "Symbol": stock_name,
                    "Current Price / LTP": "N/A",
                    "Open": "N/A",
                    "High": "N/A",
                    "Low": "N/A",
                    "Previous Close": "N/A",
                    "Volume": "N/A"
                })
    except Exception:
        for stock_name in STOCK_LIST:
            yahoo_symbol = get_yahoo_symbol(stock_name)
            try:
                ticker = yf.Ticker(yahoo_symbol)
                info = ticker.info

                current_price = info.get('currentPrice') or info.get(
                    'regularMarketPrice') or info.get('previousClose', 0)
                open_price = info.get('open') or info.get(
                    'regularMarketOpen', 0)
                high_price = info.get('dayHigh') or info.get(
                    'regularMarketDayHigh', 0)
                low_price = info.get('dayLow') or info.get(
                    'regularMarketDayLow', 0)
                prev_close = info.get('previousClose') or info.get(
                    'regularMarketPreviousClose', 0)
                volume = info.get('volume') or info.get(
                    'regularMarketVolume', 0)

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
                all_data.append({
                    "Symbol": stock_name,
                    "Current Price / LTP": "N/A",
                    "Open": "N/A",
                    "High": "N/A",
                    "Low": "N/A",
                    "Previous Close": "N/A",
                    "Volume": "N/A"
                })

    return pd.DataFrame(all_data)


def format_dataframe(df):
    formatted_df = df.copy()

    for col in [
            "Current Price / LTP", "Open", "High", "Low", "Previous Close"
    ]:
        formatted_df[col] = formatted_df[col].apply(
            lambda x: f"₹{x:,.2f}"
            if isinstance(x, (int, float)) and x != 0 else x)

    formatted_df["Volume"] = formatted_df["Volume"].apply(
        lambda x: f"{x:,.0f}" if isinstance(x, (int, float)) and x != 0 else x)

    return formatted_df


def main():
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
        df = get_all_stocks_data()

    if not df.empty:
        st.subheader("📊 Stock Price Data - All Symbols")

        formatted_df = format_dataframe(df)

        st.dataframe(formatted_df, height=800, hide_index=True)

        st.markdown("---")

        csv_data = df.to_csv(index=False)
        st.download_button(
            label="📥 Download Data as CSV",
            data=csv_data,
            # The file name visible to the user remains static
            file_name="stock_data.csv",
            mime="text/csv",
            # *** MODIFICATION START ***
            # Adding a static key helps Streamlit maintain the same
            # internal API resource link across auto-refreshes.
            key="static_download_link"
            # *** MODIFICATION END ***
        )
    else:
        st.warning("Unable to fetch stock data. Please try again later.")

    st.markdown("---")
    st.markdown("""
        <div style='text-align: center; color: #666;'>
            <p>Data provided by Yahoo Finance | Auto-refreshes every 60 seconds</p>
        </div>
        """,
                unsafe_allow_html=True)


if __name__ == "__main__":
    main()
