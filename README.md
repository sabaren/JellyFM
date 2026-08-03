# JellyFM

Self-hosted internet radio powered by your [Jellyfin](https://jellyfin.org) music library. Create stations by genre, stream continuous playback with AI-generated announcements via Kokoro neural TTS, and control everything through a clean web UI.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)

---

## Features

- **Genre-based stations** — Pick from your Jellyfin genres and start listening instantly
- **Continuous broadcast** — Automatic track sequencing with TTS announcements between songs
- **Smart queue management** — 500-track buffer with append-style refill (no random jumps on skip)
- **Kokoro neural TTS** — Studio-quality offline voice synthesis with 13+ voices (American/British, male/female)
- **Radio host personas** — Blended voice profiles (Smooth Host, Warm Evening, BBC Style, Chill Late-Night)
- **Web UI** — Create stations, play/pause/skip, see now-playing info
- **REST API** — Full programmatic control with Swagger docs at `/docs`

---

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Web UI     │────▶│  FastAPI     │────▶│  Jellyfin    │
│  (Browser)   │◀────│  Backend     │◀────│  Server      │
└──────────────┘     │              │     └──────────────┘
                     │              │
                     │  ┌──────────┐│     ┌────────────┐
                     │  │ Kokoro   │◀────▶│ ONNX Runtime│
                     │  │ TTS      │     └────────────┘
                     │  └──────────┘
                     │  ┌──────────┐
                     │  │ playback │────▶ MP3 Stream
                     │  │ service  │     (port 8000)
                     │  └──────────┘
                     └──────────────┘
```

---

## Requirements

- A running [Jellyfin](https://jellyfin.org) server with music in your library
- Python 3.11+ with virtual environment support
- **[Kokoro ONNX](https://github.com/hexgrad/kokoro)** model files for TTS (see below)

---

## Quick Start

### 1. Install system dependencies

```bash
# Debian / Ubuntu / Nobara
sudo apt update
sudo apt install -y espeak python3-pip python3-venv

# Fedora / RHEL
sudo dnf install -y espeak python3-pip
```

### 2. Set up Python environment

```bash
git clone https://github.com/sabaren/JellyFM.git
cd JellyFM
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Download Kokoro TTS model

```bash
mkdir -p ~/kokoro
# Download kokoro-v1.0.onnx and voices-v1.0.bin to ~/kokoro/
# See https://github.com/hexgrad/kokoro for model download instructions
```

### 4. Configure

```bash
cp .env.example .env
# Edit .env with your Jellyfin details
```

**.env**
```env
JELLYFIN_URL=http://your-jellyfin-host:8096
JELLYFIN_USERNAME=your_username
JELLYFIN_PASSWORD=your_password

APP_HOST=0.0.0.0
APP_PORT=8000
LOG_LEVEL=info
```

### 5. Run

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser. Interactive API docs at **http://localhost:8000/docs**.

---

## Project Structure

```
JellyFM/
├── backend/
│   ├── main.py              # FastAPI app, lifespan, CORS
│   ├── models/
│   │   ├── station.py       # Station Pydantic model
│   │   └── jellyfin.py      # Track, Genre models
│   ├── routers/
│   │   ├── stations.py      # Station CRUD + playback controls
│   │   ├── health.py        # Health check + auth status
│   │   └── auth.py          # Jellyfin authentication
│   └── services/
│       ├── jellyfin.py      # Jellyfin API client
│       ├── station_manager.py  # Station registry + queue management
│       ├── playback.py      # Broadcast loop, MP3 streaming
│       ├── tts_manager.py   # TTS coordinator (Kokoro > espeak)
│       ├── kokoro.py        # Kokoro ONNX TTS service
│       ├── espeak.py        # eSpeak fallback TTS
│       └── romanize.py      # Text normalization for TTS
├── requirements.txt
├── .env.example
└── README.md
```

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Server + Jellyfin auth status |
| `POST` | `/auth` | Manually trigger Jellyfin auth |
| `GET` | `/genres` | List all genres from Jellyfin |
| `GET` | `/devices` | List audio output devices |
| `GET` | `/stations` | List all stations |
| `POST` | `/stations` | Create station `{name, genre, shuffle}` |
| `GET` | `/stations/{id}` | Station details |
| `DELETE` | `/stations/{id}` | Delete station + stop playback |
| `GET` | `/stations/{id}/now-playing` | Current track, next track, stream URL |
| `POST` | `/stations/{id}/play` | Start playback `{audio_device?}` |
| `POST` | `/stations/{id}/pause` | Toggle pause |
| `POST` | `/stations/{id}/skip` | Skip to next track |
| `POST` | `/stations/{id}/stop` | Stop playback |
| `POST` | `/stations/{id}/refill` | Re-fetch tracks from Jellyfin |

Full interactive docs at `http://localhost:8000/docs`.

---

## TTS Voices

### Base Voices (Kokoro)
| ID | Name |
|----|------|
| `af_bella` | Bella (American Female) |
| `af_nicole` | Nicole (American Female) |
| `af_sarah` | Sarah (American Female) |
| `af_sky` | Sky (American Female) |
| `am_adam` | Adam (American Male) |
| `am_michael` | Michael (American Male) |
| `bf_emma` | Emma (British Female) |
| `bf_isabella` | Isabella (British Female) |
| `bm_george` | George (British Male) |
| `bm_lewis` | Lewis (British Male) |

### Radio Host Blends
| ID | Name | Blend |
|----|------|-------|
| `blend_smooth_host` | Smooth Radio Host | Adam 60% + Michael 40% |
| `blend_warm_host` | Warm Evening Host | Bella 70% + Sarah 30% |
| `blend_bbc_host` | BBC Style Host | George 55% + Emma 45% |
| `blend_chill_host` | Chill Late-Night Host | Sky 50% + Adam 30% + Nicole 20% |

Fallback to **eSpeak** (6 voices) if Kokoro is unavailable.

---

## Docker

### Dockerfile

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    espeak \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /root/kokoro
# COPY your kokoro-v1.0.onnx and voices-v1.0.bin to /root/kokoro/

EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Build and run

```bash
docker build -t jellyfm .

docker run -d \
  --name jellyfm \
  -p 8000:8000 \
  --env-file .env \
  jellyfm
```

### Docker Compose

```yaml
services:
  jellyfm:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    restart: unless-stopped
```

```bash
docker compose up -d
```

---

## Recent Changes

### v0.2.0 — Queue & Stability
- **Fixed skip behavior:** `refill_queue()` now appends tracks instead of replacing the entire queue — no more random jumps
- **Queue size:** Increased from 200 → 500 tracks for heavy skip buffer
- **TTS warmup:** Moved to background thread — server starts in seconds
- **Broadcast stability:** Improved playback loop resilience

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Kokoro TTS not available` | Ensure `~/kokoro/kokoro-v1.0.onnx` and `~/kokoro/voices-v1.0.bin` exist |
| `Jellyfin auth failed` | Check `JELLYFIN_URL`, username, password in `.env` |
| Server hangs on startup | This was fixed in v0.2.0 — warmup now runs on background thread |
| Skip plays random songs | Fixed in v0.2.0 — queue refill now appends instead of replacing |

---

## License

MIT © [Nicholaus Sabare](https://github.com/sabaren)
