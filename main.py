from flask import Flask, jsonify
import requests
import os

app = Flask(__name__)

# Tickers for NSE (no .NS required)
STOCK_TICKERS = [
    'INFY',
    'TCS',
    'RELIANCE',
    'HDFCBANK'
]

# NSE API endpoints
NSE_HOME = "https://www.nseindia.com/"
NSE_QUOTE_API = "https://www.nseindia.com/api/quote-equity?symbol="

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
}

def get_session():
    """Establish NSE session & cookies"""
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=10)
    return s


def fetch_stock_data_raw(ticker_list):

    table_headers = ["Symbol", "LTP", "Open", "High", "Low", "Prev Close", "Volume"]
    table_data = []

    try:
        session = get_session()
    except Exception as e:
        return {"error": f"NSE Session Failed: {e}"}

    for ticker in ticker_list:
        try:
            url = NSE_QUOTE_API + ticker
            r = session.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()

            priceInfo = data.get("priceInfo", {})
            securityInfo = data.get("securityInfo", {})
            orderBookInfo = data.get("marketDeptOrderBook", {}).get("tradeInfo", {})
            marketStatus = data.get("marketStatus", {}).get("marketStatus", "Unknown")

            # Extract values safely
            last_price = priceInfo.get("lastPrice", priceInfo.get("close", 0))
            open_price = priceInfo.get("open", 0)
            high_price = priceInfo.get("intraDayHighLow", {}).get("max", 0)
            low_price = priceInfo.get("intraDayHighLow", {}).get("min", 0)
            prev_close = securityInfo.get("prevClose", 0)
            volume = orderBookInfo.get("totalTradedVolume", 0)

            table_data.append([
                ticker,
                float(last_price),
                float(open_price),
                float(high_price),
                float(low_price),
                float(prev_close),
                int(volume)
            ])

        except Exception as e:
            table_data.append([ticker, f"Error: {str(e)}", 0, 0, 0, 0, 0])

    return {"headers": table_headers, "data": table_data}


@app.route("/")
def home():
    return "NSE Live Stock API Running!", 200


@app.route("/get-live-data")
def get_live_data():
    data = fetch_stock_data_raw(STOCK_TICKERS)
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
            
