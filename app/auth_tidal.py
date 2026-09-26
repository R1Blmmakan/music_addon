"""
Tidal Device Authorization CLI
Performs standard OAuth 2.0 Device Code pairing for Tidal accounts.
Generates token.json for the native Tidal provider.
"""
import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path
import httpx

AUTH_CLIENT_ID = "fX2JxdmntZWK0ixT"
AUTH_CLIENT_SECRET = "1Nm5AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg="
DEVICE_AUTH_URL = "https://auth.tidal.com/v1/oauth2/device_authorization"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
TOKEN_FILE = Path(os.getenv("TIDAL_TOKEN_FILE", Path(__file__).resolve().parent.parent / "token.json"))

async def authenticate():
    print("=" * 60)
    print("   Tidal HiFi Lossless Account Pairing (OAuth 2.0)")
    print("=" * 60)
    print("Requesting device pairing code from Tidal...")

    data = {
        "client_id": AUTH_CLIENT_ID,
        "scope": "r_usr+w_usr+w_sub"
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(DEVICE_AUTH_URL, data=data)
        except Exception as e:
            print(f"[!] Network error connecting to Tidal: {e}")
            sys.exit(1)

        if resp.status_code != 200:
            print(f"[!] Error requesting authorization: {resp.status_code} - {resp.text}")
            sys.exit(1)

        auth_data = resp.json()
        device_code = auth_data["deviceCode"]
        user_code = auth_data["userCode"]
        verify_url = auth_data.get("verificationUriComplete") or f"https://link.tidal.com/{user_code}"
        if not verify_url.startswith("http"):
            verify_url = f"https://{verify_url}"

        print("\n" + "-" * 60)
        print(f" Pairing URL : {verify_url}")
        print(f" User Code   : {user_code}")
        print("-" * 60)
        print("Opening browser for authorization...")
        try:
            webbrowser.open(verify_url)
        except Exception:
            pass

        print("Awaiting confirmation from your Tidal account (press Ctrl+C to cancel)...")
        token_payload = {
            "client_id": AUTH_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "scope": "r_usr+w_usr+w_sub"
        }
        basic_auth = (AUTH_CLIENT_ID, AUTH_CLIENT_SECRET)

        while True:
            await asyncio.sleep(auth_data.get("interval", 3))
            try:
                poll_resp = await client.post(TOKEN_URL, data=token_payload, auth=basic_auth)
                if poll_resp.status_code == 200:
                    token_info = poll_resp.json()
                    break
                elif poll_resp.status_code in (400, 401):
                    # Still waiting for user approval in browser
                    continue
                else:
                    print(f"Polling status: {poll_resp.status_code}")
            except Exception:
                continue

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

    print("\n" + "=" * 60)
    print(f"[OK] Authentication successful! Token saved to: {TOKEN_FILE}")
    print("     Your Tidal provider is now fully active.")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    try:
        asyncio.run(authenticate())
    except KeyboardInterrupt:
        print("\nPairing aborted by user.")