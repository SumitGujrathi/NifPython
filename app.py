import os
import json
import time
import requests
import schedule
import gspread
from google.oauth2.service_account import Credentials

# --- CONFIGURATION ---
SHEET_NAME = os.getenv("SPREADSHEET_NAME", "LIVE_DATA")

# Load Google Credentials from Environment Variable
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_CREDENTIALS"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

credentials = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
gc = gspread.authorize(credentials)

def fetch_nse_option_chain(symbol):
    base_url = "https://www.nseindia.com"
    api_url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.nseindia.com/option-chain"
    }

    session = requests.Session()
    session.headers.update(headers)

    try:
        # Step 1: Establish session and retrieve cookies
        session.get(base_url, timeout=10)
        time.sleep(1)

        # Step 2: Request option chain data with established session
        response = session.get(api_url, timeout=10)

        if response.status_code == 200:
            return response.json()
        else:
            print(f"[{symbol}] Failed with HTTP Status: {response.status_code}")
            return None
    except Exception as e:
        print(f"[{symbol}] Exception during fetch: {e}")
        return None

def process_and_update_sheet(symbol, sheet_tab_name):
    data = fetch_nse_option_chain(symbol)
    if not data or "records" not in data:
        print(f"No valid data retrieved for {symbol}")
        return

    underlying_val = data["records"].get("underlyingValue", 0)
    expiry_dates = data["records"].get("expiryDates", [])
    if not expiry_dates:
        print(f"No expiry dates found for {symbol}")
        return

    current_expiry = expiry_dates[0]
    updated_time = time.strftime("%m/%d/%Y %H:%M:%S")

    output_rows = [
        [f"Symbol: {symbol}", f"Underlying LTP: {underlying_val}", f"Expiry: {current_expiry}", f"Updated: {updated_time}"],
        ["CALLS OI", "CALLS Vol", "CALLS IV", "CALLS LTP", "STRIKE", "PUTS LTP", "PUTS IV", "PUTS Vol", "PUTS OI"]
    ]

    range_limit = 1200 if symbol == "BANKNIFTY" else 600
    filtered_data = [d for d in data["records"]["data"] if d.get("expiryDate") == current_expiry]

    for item in filtered_data:
        strike = item.get("strikePrice", 0)
        if abs(strike - underlying_val) <= range_limit:
            ce = item.get("CE", {})
            pe = item.get("PE", {})

            output_rows.append([
                ce.get("openInterest", 0),
                ce.get("totalTradedVolume", 0),
                ce.get("impliedVolatility", 0),
                ce.get("lastPrice", 0),
                strike,
                pe.get("lastPrice", 0),
                pe.get("impliedVolatility", 0),
                pe.get("totalTradedVolume", 0),
                pe.get("openInterest", 0)
            ])

    try:
        spreadsheet = gc.open(SHEET_NAME)
        try:
            worksheet = spreadsheet.worksheet(sheet_tab_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=sheet_tab_name, rows="100", cols="10")

        worksheet.clear()
        worksheet.update('A1', output_rows)
        print(f"[{updated_time}] Successfully updated {sheet_tab_name} in Google Sheet!")
    except Exception as e:
        print(f"Error updating Google Sheet: {e}")

def job():
    print("Starting scheduled sync...")
    process_and_update_sheet("NIFTY", "NIFTY_OC")
    process_and_update_sheet("BANKNIFTY", "BANKNIFTY_OC")

if __name__ == "__main__":
    # Run initial sync on startup
    job()

    # Schedule sync every 1 minute
    schedule.every(1).minutes.do(job)

    while True:
        schedule.run_pending()
        time.sleep(1)
