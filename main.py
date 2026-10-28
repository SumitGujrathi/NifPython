# --- AUTOMATED TOKEN GENERATION FLOW ---
def generate_upstox_token():
    """
    Automates Upstox login using the dedicated TOTP API endpoint.
    """
    if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, USER_ID, PIN, TOTP_KEY]):
        print("[Auth Error] Missing one or more required Upstox Environment Variables!")
        return None

    try:
        print("[Auth] Generating TOTP code...")
        totp_code = pyotp.TOTP(TOTP_KEY).now()

        session = requests.Session()

        # Step 1: Submit TOTP & PIN directly to Upstox TOTP API
        totp_url = "https://api.upstox.com/v2/totp-login/authorization/token"
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": CLIENT_ID
        }
        payload = {
            "totp": totp_code,
            "pin": PIN
        }

        print("[Auth Step 1] Submitting TOTP and PIN...")
        res_totp = session.post(totp_url, json=payload, headers=headers, timeout=10)
        print(f"[Auth Step 1] Status Code: {res_totp.status_code}")

        if res_totp.status_code != 200:
            print(f"[Auth Step 1 Error] TOTP verification failed: {res_totp.text}")
            return None

        # Step 2: Request Authorization Code
        encoded_redirect = urllib.parse.quote(REDIRECT_URI, safe='')
        dialog_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={CLIENT_ID}&redirect_uri={encoded_redirect}"

        print("[Auth Step 2] Requesting Authorization Dialog...")
        res_dialog = session.get(dialog_url, allow_redirects=False, timeout=10)
        
        location_header = res_dialog.headers.get("Location", "")
        if not location_header:
            print(f"[Auth Step 2 Error] No Location header returned ({res_dialog.status_code}): {res_dialog.text}")
            return None

        parsed_url = urlparse(location_header)
        auth_code = parse_qs(parsed_url.query).get("code", [None])[0]

        if not auth_code:
            print(f"[Auth Step 2 Error] Failed to extract auth code from Location header: {location_header}")
            return None

        # Step 3: Exchange Auth Code for Access Token
        token_url = "https://api.upstox.com/v2/login/authorization/token"
        token_headers = {
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        token_payload = {
            "code": auth_code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code"
        }

        print("[Auth Step 3] Exchanging Auth Code for Access Token...")
        res_token = session.post(token_url, data=token_payload, headers=token_headers, timeout=10)

        if res_token.status_code == 200:
            token = res_token.json().get("access_token")
            TOKEN_CACHE["access_token"] = token
            TOKEN_CACHE["generated_at"] = time.time()
            print("[Auth Success] Fresh Upstox Access Token generated successfully!")
            return token
        else:
            print(f"[Auth Step 3 Error] Token Exchange failed ({res_token.status_code}): {res_token.text}")

    except Exception as e:
        print(f"[Auth Exception] Exception during automated login: {e}")

    return None
