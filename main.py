import time
from playwright.sync_api import sync_playwright

def fetch_nse_option_chain_playwright(symbol):
    """
    Uses a real headless browser instance via Playwright to pass Akamai checks.
    """
    base_url = "https://www.nseindia.com"
    api_url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"

    with sync_playwright() as p:
        # Launch Chromium with anti-detection flags
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()

        try:
            # 1. Navigate to main page to trigger session cookies
            page.goto(base_url, wait_until="networkidle", timeout=15000)
            time.sleep(2)

            # 2. Fetch the API endpoint within the authenticated browser context
            response = page.goto(api_url, wait_until="networkidle", timeout=15000)
            
            if response.status == 200:
                json_data = response.json()
                browser.close()
                return json_data
            else:
                print(f"[{symbol}] Failed with status: {response.status}")
        except Exception as e:
            print(f"[{symbol}] Playwright Error: {e}")
        
        browser.close()
    return None
