from flask import Flask, jsonify
import requests
import os

app = Flask(__name__)

TICKERS = ["INFY", "TCS", "RELIANCE", "HDFCBANK"]

API = "https://nse-api-wrapper.vercel.app/stock/"

@app.route('/get-live-data')
def get_live_data():

    table_headers = ["Symbol", "LTP", "Open", "High", "Low", "Prev Close", "Volume"]
    table_data = []

    for symbol in TICKERS:

        try:
            r = requests.get(API + symbol, timeout=10)
            data = r.json()

            price = data["priceInfo"]
            security = data["securityInfo"]
            order = data["marketDeptOrderBook"]["tradeInfo"]

            table_data.append([
                symbol,
                price["lastPrice"],
                price["open"],
                price["intraDayHighLow"]["max"],
                price["intraDayHighLow"]["min"],
                security["prevClose"],
                order["totalTradedVolume"]
            ])

        except Exception as e:
            table_data.append([symbol, "Error", 0, 0, 0, 0, 0])

    return jsonify({"headers": table_headers, "data": table_data})


@app.route('/')
def home():
    return "NSE Stock API Running", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
