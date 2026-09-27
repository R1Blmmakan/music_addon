# BitChord Lossless Relay

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat&logo=FastAPI&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat&logo=Python&logoColor=white)](https://python.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?style=flat&logo=Docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A self-hosted audio relay that delivers lossless FLAC streams from Tidal and Deezer directly to the [BitChord](https://github.com/kushagrasinghx/BitChord) music player on Android.

It aggregates search results across both catalogs, decrypts Deezer Blowfish streams on the fly, resolves Tidal DASH manifests, and implements cross-provider failover so tracks play without dropping back to YouTube audio.

---

## What It Does

* **On-the-Fly Stream Decryption:** Intercepts Deezer CDN audio streams (encrypted in 2048-byte stripes using Blowfish in CBC mode), derives per-track Blowfish keys from track IDs using MD5 XOR transformations, and streams decrypted FLAC bytes directly to the client without touching disk.
* **HTTP 206 Partial Content Pass-Through:** Preserves `Range: bytes=start-end` headers across decrypted streams so Android Media3 and ExoPlayer can buffer, seek, and scrub without interruption.
* **BitChord TrackMatcher Alignment:** Appends track version indicators (such as `(Remix)`, `(Acoustic)`, and `(Live)`) and aggregates all collaborator credits into search metadata. This satisfies BitChord's internal bytecode matcher rules (`TrackMatcher$TitleParts.versions` equality and fullest artist credit matching), preventing false fallback to Opus.
* **Tidal DASH & Regional Fallback:** Proxies Tidal Hi-Res Lossless and Lossless streams as dynamic DASH MPD manifests. If a track is region-locked or unavailable in FLAC on Tidal, the relay automatically searches Deezer in the background and serves the Deezer FLAC stream.
* **Two-Hour CDN URL Cache:** Caches signed Deezer CDN stream URLs in memory to prevent repeated authentication calls when ExoPlayer makes rapid byte-range requests during playback.
* **Studio Hardware Console Dashboard:** Serves an interactive dark-mode control interface at `/` with live provider statuses, protocol diagnostics, and an in-browser connection string builder.
* **Path-Based Token Security:** Optional access token authentication protects endpoints from crawlers by requiring tokens in the path (`/{token}/manifest.json`, `/{token}/search`, `/{token}/stream/{id}`).

---

## Architecture

```
                    +---------------------------------------------+
                    |          BitChord Android Client            |
                    +---------------------------------------------+
                                           |
                         HTTPS (Cloudflare Tunnel / Reverse Proxy)
                                           |
                                           v
+---------------------------------------------------------------------------------+
|                       BitChord Lossless Relay (FastAPI)                         |
|                                                                                 |
|   +-------------------+    +----------------------+    +--------------------+   |
|   |  /{token}/manifest|    |    /{token}/search   |    |   /{token}/stream  |   |
|   | (Capability Deck) |    | (Concurrent Scrapers)|    | (Fallback Resolver)|   |
|   +-------------------+    +----------------------+    +--------------------+   |
|                                       |                           |             |
|                  +--------------------+                           |             |
|                  |                                                v             |
|                  v                                       +------------------+   |
|   +------------------------------+                       | /audio/dz/{id}   |   |
|   |      Provider Adapters       |                       | (Blowfish Proxy) |   |
|   |  +------------------------+  |                       +------------------+   |
|   |  | Tidal (DASH / FLAC)    |  |                                |             |
|   |  | Deezer (16-bit FLAC)   |  |                                |             |
|   |  +------------------------+  |                                |             |
|   +------------------------------+                                |             |
+-------------------|-----------------------------------------------|-------------+
                    |                                               |
                    v                                               v
          [Tidal Catalog API]                             [Deezer Audio CDN]
```

---

## API Contract

All main endpoints support path-prefixed authentication when `ACCESS_TOKEN` is configured:

| Method | Endpoint | Auth | Purpose |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | Public | Studio Hardware Console status dashboard and connection builder |
| `GET` | `/{token}/manifest.json` | Token | BitChord discovery handshake, declaring addon capabilities |
| `GET` | `/{token}/search?q={query}` | Token | Aggregates Tidal and Deezer searches with ranked scoring |
| `GET` | `/{token}/stream/{item_id}` | Token | Resolves track ID (`td:<id>` or `dz:<id>`) with automated fallback |
| `GET` | `/audio/dz/{track_id}` | Public* | Streams decrypted Deezer FLAC with HTTP 206 Range support |
| `GET` | `/dash/td/{track_id}.mpd` | Public* | Generates Tidal DASH MPD manifest with decrypted FLAC URLs |
| `GET` | `/health` | Public | Reports real-time status of configured music providers |

*\* Audio and DASH paths are routed with random, time-limited tokens to allow direct media player streaming without credentials in headers.*

---

## Configuration Reference

Set these variables in your `.env` file:

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `PUBLIC_HOST` | URL | `http://localhost:8000` | The public HTTPS domain used by BitChord to reach this relay. |
| `ACCESS_TOKEN` | String | *(empty)* | Secret key required in URL paths (`/{token}/...`). Leave blank to disable auth. |
| `DEEZER_ARL` | Hex (64) | *(empty)* | Active Deezer ARL cookie for lossless stream decryption. |
| `TIDAL_TOKEN_FILE` | Path | `token.json` | Path to Tidal OAuth session token storage. |
| `TIDAL_COUNTRY_CODE`| 2-letter ISO | `ID` | Country code for Tidal track catalog queries. |
| `PREFERRED_PROVIDER`| String | `tidal` | Primary provider in search results: `tidal` or `deezer`. |
| `ENABLE_FALLBACK` | Boolean | `true` | Enables automatic cross-provider fallback when playback fails. |
| `DIAG_ENABLED` | Boolean | `false` | Enables `/diag/*` diagnostic inspection routes. |
| `PORT` | Integer | `8000` | Container bind port. |

---

## Deployment

### Docker Compose (Recommended)

A standard deployment uses Docker Compose behind Cloudflare Tunnel, Caddy, or Nginx.

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/bitchord-lossless-relay.git
   cd bitchord-lossless-relay
   ```

2. Create `.env`:
   ```bash
   cp .env.example .env
   ```
   Fill in `PUBLIC_HOST`, `ACCESS_TOKEN`, and `DEEZER_ARL`.

3. Start the service:
   ```bash
   docker compose up -d --build
   ```

4. Check container health:
   ```bash
   docker compose logs -f
   ```

---

## Provider Authentication

### Deezer Setup
1. Log in to [Deezer](https://www.deezer.com) in your web browser.
2. Open Developer Tools (`F12`) > **Application** > **Cookies** > `https://www.deezer.com`.
3. Copy the value of the `arl` cookie (a 64-character string).
4. Set `DEEZER_ARL=your_arl_cookie` in `.env`.

### Tidal Setup
Tidal authentication uses an interactive OAuth login:
```bash
python app/auth_tidal.py
```
Follow the URL prompt in your browser to sign in. The script writes session tokens to `token.json`. Mount this file into your container as configured in `docker-compose.yml`.

---

## Connecting BitChord

1. Open **BitChord** on Android.
2. Go to **Settings** > **Sources** > **Add Source**.
3. Enter your connection string:
   * **With ACCESS_TOKEN set:**
     ```
     https://api.yourdomain.com/YOUR_SECRET_TOKEN
     ```
   * **Without ACCESS_TOKEN:**
     ```
     https://api.yourdomain.com
     ```
4. BitChord calls `/manifest.json`, validates available stream endpoints, and routes lossless queries through the relay.

---

## Verifying Playback

To verify stream resolution without the Android client:

```bash
# 1. Search for a track
curl -s "https://api.yourdomain.com/YOUR_SECRET_TOKEN/search?q=the+hills+the+weeknd" | jq

# 2. Request a stream URL
curl -s "https://api.yourdomain.com/YOUR_SECRET_TOKEN/stream/td:50436095" | jq

# 3. Test HTTP Range pass-through on Deezer audio
curl -I -H "Range: bytes=0-1024" "https://api.yourdomain.com/audio/dz/106506512"
```

Expected Range response:
```http
HTTP/1.1 206 Partial Content
Content-Type: audio/flac
Content-Range: bytes 0-1024/27849182
Accept-Ranges: bytes
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
