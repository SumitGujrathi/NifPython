import os
import json
import time
import requests
import pyotp
import schedule
import urllib.parse
from urllib.parse import parse_qs, urlparse
import gspread
from google.oauth2.service_account import Credentials

# --- ENVIRONMENT CONFIGURATION ---
CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID")
CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET")
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI")
USER_ID = os.getenv("UPSTOX_USER_ID")
PIN = os.getenv("UPSTOX_PIN")
TOTP_KEY = os.getenv("UPSTOX_TOTP_KEY")

SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "LIVE_DATA")
RAW_GOOGLE_CREDS = os.getenv("GOOGLE_CREDENTIALS")

def generate_upstox_access_token():
    """
    Automates Upstox OAuth2 Login Flow using pyotp to get Access Token
    """
    print("[1/4] Generating TOTP Code...")
    totp_code = pyotp.TOTP(TOTP_KEY).now()

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9"
    }
    session.headers.update(headers)

    try:
        # Step 1: Send Login OTP Request
        print("[2/4] Initiating Login Auth...")
        otp_url = "https://api.upstox.com/v2/login/auth/token/request/otp"
        otp_payload = {"user_id": USER_ID}
        res1 = session.post(otp_url, json=otp_payload, timeout=10)
        
        # Step 2: Submit PIN and TOTP
        print("[3/4] Submitting PIN and TOTP...")
        auth_url = "https://api.upstox.com/v2/login/auth/token/verify"
        auth_payload = {
            "user_id": USER_ID,
            "pin": PIN,
            "otp": totp_code
        }
        res2 = session.post(auth_url, json=auth_payload, timeout=10)
        
        # Step 3: Get Auth Code from Authorization Dialog
        dialog_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={CLIENT_ID}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        res3 = session.get(dialog_url, allow_redirects=False, timeout=10)
        
        location_header = res3.headers.get("Location", "")
        parsed_url = urlparse(location_header)
        auth_code = parse_qs(parsed_url.query).get("code", [None])[0]

        if not auth_code:
            print(f"Failed to extract authorization code. Location: {location_header}")
            return None

        # Step 4: Exchange Auth Code for Access Token
        print("[4/4] Exchanging Auth Code for Access Token...")
        token_url = "https://api.upstox.com/v2/login/auth/token"
        token_payload = {
            "code": auth_code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code"
        }
        
        res4 = session.post(token_url, data=token_payload, timeout=10)
        if res4.status_code == 200:
            access_token = res4.json().get("access_token")
            print("Successfully generated new Upstox Access Token!")
            return access_token
        else:
            print(f"Token Error: {res4.status_code} - {res4.text}")

    except Exception as e:
        print(f"Exception during Upstox Auto-Login: {e}")

    return None

def update_google_sheet_token(access_token):
    """
    Writes the fresh Access Token into 'TOKEN' tab in Google Sheet
    """
    if not access_token:
        print("Skipping Google Sheet update because Access Token is invalid.")
        return

    try:
        creds_dict = json.loads(RAW_GOOGLE_CREDS)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(credentials)

        spreadsheet = gc.open(SPREADSHEET_NAME)
        try:
            worksheet = spreadsheet.worksheet("TOKEN")
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title="TOKEN", rows="10", cols="5")

        updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        worksheet.update('A1', [[access_token]])
        worksheet.update('B1', [[f"Updated: {updated_at}"]])
        print(f"Updated cell A1 in 'TOKEN' tab successfully at {updated_at}!")

    except Exception as e:
        print(f"Error updating Google Sheet: {e}")

def run_token_job():
    print("Starting daily token refresh job...")
    token = generate_upstox_access_token()
    if token:
        update_google_sheet_token(token)

if __name__ == "__main__":
    # Execute immediately on container startup
    run_token_job()

    # Schedule job every morning at 08:00 AM IST
    schedule.every().day.at("02:30").do(run_token_job)  # 02:30 UTC = 08:00 AM IST

    while True:
        schedule.run_pending()
        time.sleep(30)
