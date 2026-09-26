# BitChord Lossless Relay

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat&logo=FastAPI&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat&logo=Python&logoColor=white)](https://python.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?style=flat&logo=Docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A self-hosted, high-performance audio relay and streaming proxy built for the [BitChord](https://github.com/kushagrasinghx/BitChord) music player on Android. It bridges multiple HiFi streaming platforms (Tidal and Deezer) into a single unified endpoint with real-time Blowfish stream decryption, dynamic cross-provider fallback, and HTTP 206 seeking support.

---

## Key Engineering Highlights

* **Real-time Cipher Decryption:** Deezer streams audio via CDN in 2048-byte stripes encrypted with Blowfish in CBC mode. This service intercepts raw CDN streams, computes track-specific keys via MD5 XOR manipulation, and decrypts audio blocks on the fly without writing bytes to disk.
* **HTTP 206 Range Request Pass-through:** Android Media3 and ExoPlayer require byte-range requests (`Range: bytes=start-end`) for playback buffering and scrubber seeking. The relay preserves byte offsets across decrypted streams.
* **Namespace Routing with Automatic Fallback:** Tracks are namespaced by origin (`td:<id>` for Tidal, `dz:<id>` for Deezer). If Tidal rate-limits or returns a region-lock error, the resolver catches the failure, cross-searches Deezer in the background, and returns an alternate FLAC stream.
* **BitChord Protocol Compliance:** Full implementation of BitChord's 3-endpoint discovery specification (`/manifest.json`, `/search`, and `/stream/{id}`).
* **Built-in Status Dashboard:** Visiting the root URL (`/`) serves an interactive dark-mode dashboard displaying live provider health, protocol diagnostics, and configuration state.

---

## System Architecture

```
                   +---------------------------------------------+
                   |          BitChord Android Client            |
                   +---------------------------------------------+
                                          |
                      (HTTPS via Cloudflare Tunnel / Caddy)
                                          |
                                          v
+---------------------------------------------------------------------------------+
|                       BitChord Lossless Relay (FastAPI)                         |
|                                                                                 |
|   +-------------------+    +----------------------+    +--------------------+   |
|   |   /manifest.json  |    |        /search       |    |    /stream/{id}    |   |
|   | (Addon Handshake) |    | (Concurrent Scrapers)|    | (Fallback Resolver)|   |
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
+---|------------------------------|--------------------------------|-------------+
    |                              |                                |
    v                              v                                v
[Tidal API / CDN]       [Deezer Gateway & API]            [Deezer Audio CDN]
```

---

## API Specification

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Web status dashboard and connection guide |
| `GET` | `/manifest.json` | Discovery handshake exposing source ID and quality options |
| `GET` | `/search?q={query}` | Concurrent search across configured backends, sorted by quality |
| `GET` | `/stream/{id}` | Resolves track ID to playable URL with cross-provider fallback |
| `GET` | `/audio/dz/{track_id}` | Decrypts and streams Deezer audio with HTTP Range support |
| `GET` | `/health` | JSON diagnostics reporting status of each provider |
| `GET` | `/docs` | Interactive Swagger / OpenAPI documentation |

---

## Quickstart

### Prerequisites
* Python 3.11 or newer (or Docker)
* An active Deezer account (ARL cookie from browser storage)
* (Optional) An active Tidal session if using Tidal Hi-Res

### Local Development

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/bitchord-lossless-relay.git
   cd bitchord-lossless-relay
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Configure environment:
   ```bash
   cp .env.example .env
   ```
   Add your `DEEZER_ARL` token in `.env`.

4. Start the server:
   ```bash
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```
   Open `http://localhost:8000` to inspect the status dashboard.

---

## Deployment (Docker & Homelab)

A production-ready `docker-compose.yml` is included for deployment on ZimaOS, Unraid, CasaOS, or Debian:

```bash
docker compose up -d --build
```

### Exposing with HTTPS
Android enforces HTTPS by default. Route incoming traffic through Cloudflare Tunnel (Zero Trust) or a reverse proxy (Caddy / Nginx) pointing to container port `8000`.

---

## Connecting to BitChord

1. Open **BitChord** on your Android device.
2. Navigate to **Settings** > **Sources** > **Add Source**.
3. Enter your public HTTPS URL:
   ```
   https://music.yourdomain.com
   ```
4. BitChord will query `/manifest.json`, verify `search` and `stream` capabilities, and activate the HiFi relay.

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
