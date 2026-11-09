"""Tidal Account Authentication CLI.

Supports:
1. Android PKCE Flow (RECOMMENDED):
   Unlocks both 16-bit CD-Quality FLAC ('LOSSLESS') and 24-bit Hi-Res FLAC ('HI_RES_LOSSLESS').
   Works in headless environments (Docker / ZimaOS / remote servers).
2. Legacy Device Code Pairing:
   Standard TV OAuth pairing (24-bit HiRes DASH FLAC works, but 16-bit FLAC downgrades to 320k AAC).

Writes directly to TIDAL_TOKEN_FILE (default: data/token.json, or /app/data/token.json in Docker).
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

# ── Credentials ──────────────────────────────────────────────────────────────
# Android PKCE Client (issues 'at: INTERNAL' tokens that unlock 16-bit + 24-bit FLAC)
CLIENT_ID_PKCE = base64.b64decode(
    base64.b64decode(b"TmtKRVUxSmtjRXM=") + base64.b64decode(b"NWFIRkZRbFJuVlE9PQ==")
).decode("utf-8")
CLIENT_SECRET_PKCE = base64.b64decode(
    base64.b64decode(b"ZUdWMVVHMVpOMjVpY0ZvNVNVbGlURUZqVVQ=")
    + base64.b64decode(b"a3pjMmhyWVRGV1RtaGxWVUZ4VGpaSlkzTjZhbFJIT0QwPQ==")
).decode("utf-8")

# Legacy TV Device Client
CLIENT_ID_DEVICE = os.getenv("TIDAL_CLIENT_ID", "fX2JxdmntZWK0ixT")
CLIENT_SECRET_DEVICE = os.getenv("TIDAL_CLIENT_SECRET", "1Nn9AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg=")

AUTH_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
DEVICE_AUTH_URL = "https://auth.tidal.com/v1/oauth2/device_authorization"
REDIRECT_URI = "https://tidal.com/android/login/auth"
SCOPE = "r_usr+w_usr+w_sub"

# Resolve token output path
default_token_path = Path(__file__).resolve().parent.parent / "data" / "token.json"
TOKEN_FILE = Path(os.getenv("TIDAL_TOKEN_FILE", default_token_path))
STATE_FILE = TOKEN_FILE.parent / ".pkce_state.json"


def _generate_pkce_challenge() -> tuple[str, str, str]:
    cuk = format(random.getrandbits(64), "02x")
    verifier = base64.urlsafe_b64encode(os.urandom(32))[:-1].decode("utf-8")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    )[:-1].decode("utf-8")
    return cuk, verifier, challenge


def get_pkce_auth_url(cuk: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID_PKCE,
        "lang": "EN",
        "appMode": "android",
        "client_unique_key": cuk,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "restrict_signup": "true",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _save_token(token_info: dict, client_id: str, client_secret: str, auth_type: str) -> None:
    user_info = token_info.get("user", {}) or {}
    user_id = user_info.get("userId") or token_info.get("user_id") or 0
    country_code = user_info.get("countryCode") or "ID"

    saved_payload = {
        "token_type": token_info.get("token_type", "Bearer"),
        "access_token": token_info["access_token"],
        "refresh_token": token_info.get("refresh_token", ""),
        "user_id": user_id,
        "country_code": country_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "expires_in": token_info.get("expires_in", 86400),
        "auth_type": auth_type,
    }

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(saved_payload, f, indent=2)

    if STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
        except Exception:
            pass

    print("\n" + "=" * 65)
    print(" [OK] Authentication SUCCESSFUL!")
    print(f"      Session written to: {TOKEN_FILE.resolve()}")
    print(f"      User ID:            {user_id}")
    print(f"      Country Code:       {country_code}")
    print(f"      Client ID:          {client_id} ({auth_type})")
    print("=" * 65)
    print("[*] To apply the updated token immediately, restart your container:")
    print("    sudo docker compose restart\n")


def exchange_pkce_code(code_or_url: str, verifier: str, cuk: str) -> None:
    code = code_or_url.strip()
    if "code=" in code:
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(code).query)
        if "code" in parsed and parsed["code"]:
            code = parsed["code"][0]

    if not code:
        print("[!] Error: No auth code could be extracted from input.")
        sys.exit(1)

    data = {
        "code": code,
        "client_id": CLIENT_ID_PKCE,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_verifier": verifier,
        "client_unique_key": cuk,
    }
    headers = {
        "User-Agent": "okhttp/5.3.2",
        "Accept": "application/json",
    }

    print("\n[*] Exchanging authorization code with Tidal...")
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(TOKEN_URL, data=data, headers=headers)
        if resp.status_code != 200:
            print(f"[!] Token exchange failed ({resp.status_code}): {resp.text}")
            sys.exit(1)
        token_resp = resp.json()

    _save_token(token_resp, CLIENT_ID_PKCE, CLIENT_SECRET_PKCE, "PKCE")


def auth_pkce_interactive() -> None:
    cuk, verifier, challenge = _generate_pkce_challenge()
    auth_url = get_pkce_auth_url(cuk, challenge)

    print("\n" + "=" * 65)
    print("     TIDAL Android PKCE Authentication")
    print("     (Unlocks 16-bit CD-Quality FLAC + 24-bit Hi-Res FLAC)")
    print("=" * 65)
    print("\n1. Open this link in your browser and log in to your Tidal account:\n")
    print(f"   {auth_url}\n")
    print("2. After you log in, your browser will redirect to a blank or 'Oops' page")
    print(f"   starting with: {REDIRECT_URI}?code=...\n")

    try:
        redirected = input("3. Copy the FULL URL from your browser's address bar and paste here: ").strip()
    except EOFError:
        print("\n[!] Interactive input not detected.")
        print("    If running inside Docker, ensure you include '-it':")
        print("    sudo docker compose exec -it unified-addon python app/auth_tidal.py\n")
        sys.exit(1)

    if not redirected:
        print("[!] No URL entered. Aborted.")
        sys.exit(1)

    exchange_pkce_code(redirected, verifier, cuk)


async def auth_device_code() -> None:
    print("\n" + "=" * 65)
    print("     TIDAL Device Code Pairing (Legacy TV Client)")
    print("=" * 65)

    headers = {
        "User-Agent": "okhttp/5.3.2",
        "Accept": "application/json",
        "X-Platform": "android",
    }

    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        print("\n[*] Requesting fresh pairing code from Tidal...")
        data = {
            "client_id": CLIENT_ID_DEVICE,
            "scope": SCOPE,
        }
        resp = await client.post(DEVICE_AUTH_URL, data=data)
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

        token_payload = {
            "client_id": CLIENT_ID_DEVICE,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "scope": SCOPE,
        }
        if CLIENT_SECRET_DEVICE and CLIENT_ID_DEVICE != "zU4XHVVkc2tDPo4t":
            token_payload["client_secret"] = CLIENT_SECRET_DEVICE

        start_time = time.time()
        while (time.time() - start_time) < expires_in:
            remaining = int(expires_in - (time.time() - start_time))
            sys.stdout.write(f"\rWaiting for authorization... [{remaining}s remaining]   ")
            sys.stdout.flush()

            await asyncio.sleep(interval)
            try:
                poll_resp = await client.post(TOKEN_URL, data=token_payload)
                if poll_resp.status_code == 200:
                    token_info = poll_resp.json()
                    _save_token(token_info, CLIENT_ID_DEVICE, CLIENT_SECRET_DEVICE, "DEVICE_CODE")
                    return

                try:
                    poll_data = poll_resp.json()
                except Exception:
                    continue

                err = poll_data.get("error", "")
                if err == "authorization_pending":
                    continue
                elif err == "slow_down":
                    interval += 2
                    continue
                elif err in ("expired_token", "invalid_grant"):
                    print(f"\n[!] Code {user_code} expired on Tidal.")
                    break
            except Exception:
                continue

        print("\n[!] Authorization timed out.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tidal Account Authentication CLI")
    parser.add_argument("--pkce", action="store_true", help="Use Android PKCE flow (default)")
    parser.add_argument("--device", action="store_true", help="Use legacy Device Code pairing")
    parser.add_argument("--url", action="store_true", help="Print PKCE login URL and save temporary state")
    parser.add_argument("--code", type=str, help="Exchange authorization code or redirect URL saved from --url")

    args = parser.parse_args()

    # Step 1 of non-interactive flow: generate URL and save state
    if args.url:
        cuk, verifier, challenge = _generate_pkce_challenge()
        auth_url = get_pkce_auth_url(cuk, challenge)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"cuk": cuk, "verifier": verifier}), encoding="utf-8")
        print("\n" + "=" * 65)
        print("  OPEN THIS URL IN YOUR BROWSER AND LOG IN:")
        print("=" * 65)
        print(f"\n{auth_url}\n")
        print("After logging in, copy the redirect URL and complete with:")
        print("  python app/auth_tidal.py --code \"<pasted_url>\"\n")
        return

    # Step 2 of non-interactive flow: exchange code using saved state
    if args.code:
        if not STATE_FILE.exists():
            print(f"[!] Error: State file {STATE_FILE} not found. Run with --url first.")
            sys.exit(1)
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        exchange_pkce_code(args.code, state["verifier"], state["cuk"])
        return

    if args.device:
        asyncio.run(auth_device_code())
        return

    # Default interactive flow
    auth_pkce_interactive()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nAuthentication cancelled by user.")
        sys.exit(0)
