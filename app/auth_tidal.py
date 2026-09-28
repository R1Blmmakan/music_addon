"""
Tidal Device Authorization CLI
Performs standard OAuth 2.0 Device Code pairing for Tidal accounts.
Generates token.json for the native Tidal provider.
"""
import asyncio
import json
import os
import sys
import time
import webbrowser
from pathlib import Path
import httpx

AUTH_CLIENT_ID = os.getenv("TIDAL_CLIENT_ID", "zU4XHVVkc2tDPo4t")
AUTH_CLIENT_SECRET = os.getenv("TIDAL_CLIENT_SECRET", "VJKhDFqJPqvsPVNBV6ukXTJmwlvbttP7wlMlrc72se4=")
DEVICE_AUTH_URL = "https://auth.tidal.com/v1/oauth2/device_authorization"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
TOKEN_FILE = Path(os.getenv("TIDAL_TOKEN_FILE", Path(__file__).resolve().parent.parent / "token.json"))

HEADERS = {
    "User-Agent": "okhttp/5.3.2",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Platform": "android",
}

async def authenticate():
    print("\n" + "=" * 65)
    print("       TIDAL HiFi Account Pairing (OAuth 2.0)")
    print("=" * 65)

    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
        while True:
            print("\n[*] Requesting fresh pairing code from Tidal...")
            data = {
                "client_id": AUTH_CLIENT_ID,
                "scope": "r_usr+w_usr+w_sub"
            }

            try:
                resp = await client.post(DEVICE_AUTH_URL, data=data)
            except Exception as e:
                print(f"[!] Network error: {e}")
                sys.exit(1)

            if resp.status_code != 200:
                print(f"[!] Error from Tidal: {resp.status_code} - {resp.text}")
                sys.exit(1)

            auth_data = resp.json()
            device_code = auth_data["deviceCode"]
            user_code = auth_data["userCode"]
            expires_in = auth_data.get("expiresIn", 300)
            interval = max(auth_data.get("interval", 3), 4)

            verify_url = f"https://link.tidal.com/{user_code}"

            print("\n" + "#" * 65)
            print("  PLEASE OPEN THIS LINK IN YOUR BROWSER NOW:")
            print(f"  --> {verify_url}")
            print(f"  Code: {user_code} (pre-filled automatically)")
            print("#" * 65)
            print("[*] Note: Keep this terminal open while you click 'Continue' / 'Link'.\n")

            try:
                webbrowser.open(verify_url)
            except Exception:
                pass

            token_payload = {
                "client_id": AUTH_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "scope": "r_usr+w_usr+w_sub"
            }
            basic_auth = (AUTH_CLIENT_ID, AUTH_CLIENT_SECRET)

            start_time = time.time()
            success = False

            while (time.time() - start_time) < expires_in:
                remaining = int(expires_in - (time.time() - start_time))
                sys.stdout.write(f"\rWaiting for authorization... [{remaining}s remaining]   ")
                sys.stdout.flush()

                await asyncio.sleep(interval)

                try:
                    poll_resp = await client.post(TOKEN_URL, data=token_payload, auth=basic_auth)
                    if poll_resp.status_code == 200:
                        token_info = poll_resp.json()
                        success = True
                        break
                    
                    poll_data = poll_resp.json()
                    err = poll_data.get("error", "")

                    if err == "authorization_pending":
                        continue
                    elif err == "slow_down":
                        interval += 2
                        continue
                    elif err in ("expired_token", "invalid_grant"):
                        print(f"\n[!] Code {user_code} expired on Tidal.")
                        break
                    else:
                        print(f"\n[!] Polling response: {poll_data}")
                except Exception:
                    continue

            if success:
                print("\n\n" + "=" * 65)
                print(f"[OK] Authentication SUCCESSFUL!")
                saved_payload = {
                    "access_token": token_info["access_token"],
                    "refresh_token": token_info.get("refresh_token", ""),
                    "user_id": token_info.get("user", {}).get("userId", 0),
                    "client_id": AUTH_CLIENT_ID,
                    "client_secret": AUTH_CLIENT_SECRET,
                    "expires_in": token_info.get("expires_in", 604800)
                }

                TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    json.dump(saved_payload, f, indent=2)

                print(f"     Session saved to: {TOKEN_FILE}")
                print("     Your Tidal provider is now active and ready.")
                print("=" * 65 + "\n")
                return

            print("\n[!] Time expired before approval. Generating a fresh code in 3 seconds...")
            await asyncio.sleep(3)

if __name__ == "__main__":
    try:
        asyncio.run(authenticate())
    except KeyboardInterrupt:
        print("\n\nPairing cancelled by user.")