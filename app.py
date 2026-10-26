import requests

def get_yfinance_session():
    """Creates a requests session with realistic browser headers to bypass Yahoo blocks."""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    })
    return session

def fetch_stock_data_with_retries(max_retries=5, delay_between_retries=3):
    """
    Attempts to fetch stock data with session header spoofing and retries.
    """
    yahoo_symbols = [get_yahoo_symbol(s) for s in STOCK_LIST]
    df_batch = pd.DataFrame()
    session = get_yfinance_session()

    # Step 1: Attempt Batch Download with Session
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"Batch fetch attempt {attempt}/{max_retries}...")
            df_batch = yf.download(
                tickers=yahoo_symbols,
                period="5d",
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                timeout=10,
                session=session
            )
            if not df_batch.empty:
                logging.info("Batch download successful!")
                break
        except Exception as e:
            logging.warning(f"Batch fetch failed on attempt {attempt}: {str(e)}")
            time.sleep(delay_between_retries)

    all_data = []

    # Step 2: Extract data
    for stock_name in STOCK_LIST:
        yahoo_symbol = get_yahoo_symbol(stock_name)
        current_price = open_price = high_price = low_price = prev_close = volume = None
        ticker_df = pd.DataFrame()

        try:
            if not df_batch.empty:
                if isinstance(df_batch.columns, pd.MultiIndex):
                    if yahoo_symbol in df_batch.columns.get_level_values(0):
                        ticker_df = df_batch[yahoo_symbol].dropna(subset=['Close'])
                    elif yahoo_symbol in df_batch.columns.get_level_values(1):
                        ticker_df = df_batch.xs(yahoo_symbol, level=1, axis=1).dropna(subset=['Close'])
                else:
                    ticker_df = df_batch.dropna(subset=['Close'])
        except Exception:
            pass

        # Parse extracted rows safely
        try:
            if not ticker_df.empty:
                latest = ticker_df.iloc[-1]
                current_price = float(latest['Close'])
                open_price = float(latest['Open'])
                high_price = float(latest['High'])
                low_price = float(latest['Low'])
                volume = float(latest['Volume']) if 'Volume' in latest and pd.notna(latest['Volume']) else None
                prev_close = float(ticker_df.iloc[-2]['Close']) if len(ticker_df) >= 2 else open_price
        except Exception as e:
            logging.error(f"Error parsing data for {stock_name}: {str(e)}")

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
