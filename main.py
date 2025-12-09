from flask import Flask, jsonify
import requests
import os
import json
import time

app = Flask(__name__)

# NSE Allowed Symbols (without .NS)
STOCK_TICKERS = ['INFY', 'TCS', 'RELIANCE', 'HDFCBANK']

BASE_URL = "https://www.nseindia.com/"
QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol="

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/get-quotes/equity?symbol=INFY",
    "Origin": "https://www.nseindia.com",
    "Host": "www.nseindia.com",
    "Connection": "keep-alive"
}


def get_nse_session():
    """Create a session with NSE and load cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE_URL, timeout=10)  # Load cookies

    return session


def fetch_stock(ticker, session):
    """Fetch reliable JSON from NSE with retry logic."""

    url = QUOTE_URL + ticker

    for attempt in range(4):
        try:
            response = session.get(url, timeout=10)
            if response.status_code != 200:
                time.sleep(1)
                continue

            # NSE sometimes returns HTML → check for JSON
            if response.text.strip().startswith("<"):
                time.sleep(1)
                continue

            return response.json()

        except Exception:
            time.sleep(1)

    return None  # all attempts failed


@app.route('/get-live-data')
def get_live_data():

    session = get_nse_session()

    output = {
        "headers": ["Symbol", "LTP", "Open", "High", "Low", "Prev Close", "Volume"],
        "data": []
    }

    for ticker in STOCK_TICKERS:

        json_data = fetch_stock(ticker, session)

        if json_data is None:
            output["data"].append([ticker, "NSE Blocked", 0, 0, 0, 0, 0])
            continue

        try:
            price = json_data["priceInfo"]
            secInfo = json_data["securityInfo"]
            orderBook = json_data["marketDeptOrderBook"]["tradeInfo"]

            row = [
                ticker,
                price.get("lastPrice", 0),
                price.get("open", 0),
                price.get("intraDayHighLow", {}).get("max", 0),
                price.get("intraDayHighLow", {}).get("min", 0),
                secInfo.get("prevClose", 0),
                orderBook.get("totalTradedVolume", 0)
            ]

            output["data"].append(row)

        except Exception:
            output["data"].append([ticker, "Parse Error", 0, 0, 0, 0, 0])

    return jsonify(output)


@app.route('/')
def home():
    return "NSE Live API Running!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
                
