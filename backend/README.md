# Backend — TikTok Downloader API

Python FastAPI + yt-dlp. No database, no login, no secrets.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Liveness check → `{"ok": true}` |
| GET | `/api/info?url=` | Video metadata as JSON (title, author, thumbnail, duration). Downloads nothing. |
| GET | `/api/download?url=` | Downloads best-quality mp4 (watermark-free when TikTok serves it) and streams it back. Temp file is deleted after the response. |
| GET | `/` | Serves the frontend (`../frontend/index.html`) — one process hosts site + API. |

## Guards

- **URL validation** — only `https://` TikTok hosts (`tiktok.com`, `vt.tiktok.com`, `vm.tiktok.com`, …). Everything else → 400.
- **Rate limit** — 10 requests/minute per IP (in-memory, resets on restart).
- **Size cap** — videos over ~100 MB are refused (413), both at info time and after download.
- **No playlists** — `noplaylist` is forced so one link can't trigger a bulk download.

## Local run

```bash
cd backend
pip install -r requirements.txt
# ffmpeg is required by yt-dlp for some merges:
#   Ubuntu/Debian: sudo apt install ffmpeg
#   macOS: brew install ffmpeg
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000

## Deploy notes

- **Render (free, no card needed):** New → Web Service → connect repo, build `pip install -r backend/requirements.txt`, start `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`. Free tier sleeps when idle (~50s cold start) and gives ~750h/month.
- **Railway / VPS:** same commands; on a VPS make sure `ffmpeg` is installed and consider a process manager (`systemd`) + reverse proxy (nginx/caddy).
- **Heads-up:** TikTok often blocks datacenter IPs. If `/api/info` starts failing on the host but works from your laptop, that's why — the only fix is a residential proxy or a different host, both of which cost money. See the root README's Limitations section.
