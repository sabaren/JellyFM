# JellyFM
Self-hosted radio station powered by Jellyfin. Streams music by genre, announces tracks via TTS, and serves a web UI at `http://localhost:8000`.

---

## Requirements

- A running [Jellyfin](https://jellyfin.org) server with music in your library
- Python 3.11+ **or** Docker

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```env
JELLYFIN_URL=http://your-jellyfin-host:8096
JELLYFIN_USERNAME=your_username
JELLYFIN_PASSWORD=your_password

APP_HOST=0.0.0.0
APP_PORT=8000
LOG_LEVEL=info
```

---

## Local Deployment — Linux

### 1. Install system dependencies

VLC and a TTS engine are required:

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y vlc libvlc-dev espeak python3-pip

# Fedora / RHEL
sudo dnf install -y vlc vlc-devel espeak python3-pip
```

### 2. Set up Python environment

```bash
git clone https://github.com/sabaren/JellyFM.git
cd JellyFM
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure and run

```bash
cp .env.example .env
# edit .env with your Jellyfin details
python run.py
```

Open `http://localhost:8000` in your browser.

---

## Local Deployment — Windows

### 1. Install prerequisites

- [Python 3.11+](https://www.python.org/downloads/) — check **Add to PATH** during install
- [VLC media player](https://www.videolan.org/vlc/) — install the **64-bit** version

> **Important:** python-vlc looks for `libvlc.dll` on your PATH. After installing VLC, add its folder (usually `C:\Program Files\VideoLAN\VLC`) to your system PATH, or the app will fail to start.

### 2. Set up Python environment

Open **Command Prompt** or **PowerShell**:

```powershell
git clone https://github.com/sabaren/JellyFM.git
cd JellyFM
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure and run

```powershell
copy .env.example .env
# open .env in Notepad and fill in your Jellyfin details
python run.py
```

Open `http://localhost:8000` in your browser.

> **TTS on Windows:** pyttsx3 uses the built-in SAPI5 voices. No extra install needed — Windows ships with at least one voice. You can add more via **Settings → Time & Language → Speech → Add voices**.

---

## Docker

### 1. Create a `Dockerfile`

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    vlc libvlc-dev espeak \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "run.py"]
```

### 2. Build and run

```bash
docker build -t jellyfm .

docker run -d \
  --name jellyfm \
  -p 8000:8000 \
  --env-file .env \
  jellyfm
```

Open `http://localhost:8000`.

### 3. Docker Compose (optional)

Create `docker-compose.yml` alongside your `.env`:

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

> **Audio output in Docker:** containers don't have direct access to host audio hardware by default. For TTS and playback to actually produce sound on the host, you have two options:
>
> **PulseAudio (Linux host):**
> ```yaml
> volumes:
>   - /run/user/1000/pulse:/run/user/1000/pulse
> environment:
>   - PULSE_SERVER=unix:/run/user/1000/pulse/native
> ```
>
> **PipeWire (Linux host):**
> ```yaml
> volumes:
>   - /run/user/1000/pipewire-0:/run/user/1000/pipewire-0
> ```
>
> If you only need the API/UI and will handle audio routing separately, no extra config is needed.

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Server + auth status |
| `POST` | `/auth` | Manually trigger Jellyfin auth |
| `GET` | `/genres` | List all genres from Jellyfin |
| `GET` | `/devices` | List audio output devices |
| `GET` | `/stations` | List all stations |
| `POST` | `/stations` | Create a station `{name, genre, shuffle}` |
| `GET` | `/stations/{id}` | Get station details |
| `DELETE` | `/stations/{id}` | Delete station + stop playback |
| `GET` | `/stations/{id}/now-playing` | Current track, next track, stream URL |
| `POST` | `/stations/{id}/play` | Start playback `{audio_device?}` |
| `POST` | `/stations/{id}/pause` | Toggle pause |
| `POST` | `/stations/{id}/skip` | Skip to next track |
| `POST` | `/stations/{id}/stop` | Stop playback |
| `POST` | `/stations/{id}/refill` | Re-fetch tracks from Jellyfin |

Interactive docs available at `http://localhost:8000/docs`.
