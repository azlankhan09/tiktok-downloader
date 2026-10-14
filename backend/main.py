"""
TikTok Downloader — backend (FastAPI + yt-dlp).

Flow:
    browser --(paste link)--> GET /api/info?url=  --> video preview (title/thumbnail)
    browser --(click download)--> GET /api/download?url= --> mp4 file (no watermark)

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000
Then open http://localhost:8000 (the frontend is served from / too).

Notes:
- No login, no database, no secrets. Everything is stateless.
- ffmpeg must be installed on the machine (yt-dlp needs it if it ever has
  to merge separate audio/video streams). Debian/Ubuntu: apt install ffmpeg.
- TikTok does NOT offer an official download API, so this relies on yt-dlp's
  unofficial extraction. TikTok can change/break it at any time (see README).
"""

import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# ---------------------------------------------------------------------------
# Config (plain constants — nothing secret here)
# ---------------------------------------------------------------------------

# Hosts we accept links from. Anything else is rejected (also blocks SSRF-ish
# abuse where someone points the downloader at an internal/private URL).
ALLOWED_TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
}

MAX_FILESIZE_BYTES = 100 * 1024 * 1024  # ~100 MB per video
RATE_LIMIT_REQUESTS = 10                # max requests ...
RATE_LIMIT_WINDOW_S = 60                # ... per this many seconds, per IP

# In-memory hit log: {ip: [timestamp, ...]}. Simple guard against one user
# hammering the server. Resets on restart — fine for a small public site.
_rate_hits: dict[str, list[float]] = {}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

app = FastAPI(title="TikTok Downloader (no watermark)")

# CORS: harmless for a public site; lets people test the API from elsewhere.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def client_ip(request: Request) -> str:
    """Best-effort client IP (honours X-Forwarded-For behind a proxy)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(ip: str) -> None:
    """Raise 429 if this IP exceeded the per-minute budget."""
    now = time.time()
    hits = [t for t in _rate_hits.get(ip, []) if now - t < RATE_LIMIT_WINDOW_S]
    if len(hits) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Too many requests — please wait a minute and try again.",
        )
    hits.append(now)
    _rate_hits[ip] = hits


def validate_tiktok_url(raw: str) -> str:
    """
    Accept only https TikTok video/share links.
    Returns the cleaned URL or raises 400.
    """
    if not raw or len(raw) > 2048:
        raise HTTPException(status_code=400, detail="Please paste a TikTok video link.")
    parsed = urllib.parse.urlparse(raw.strip())
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Link must start with https://")
    host = parsed.netloc.lower().split(":")[0]
    if host not in ALLOWED_TIKTOK_HOSTS and not host.endswith(".tiktok.com"):
        raise HTTPException(
            status_code=400,
            detail="Only TikTok links are supported (tiktok.com, vt.tiktok.com, vm.tiktok.com).",
        )
    if not parsed.path or parsed.path == "/":
        raise HTTPException(status_code=400, detail="That doesn't look like a video link.")
    return parsed.geturl()


def friendly_download_error(exc: Exception) -> HTTPException:
    """Map yt-dlp errors to user-friendly HTTP errors."""
    msg = str(exc).lower()
    if "private" in msg:
        return HTTPException(status_code=404, detail="This video is private or was deleted.")
    if "login" in msg or "sign in" in msg:
        return HTTPException(status_code=403, detail="TikTok requires login for this video — can't download it.")
    if "not available" in msg or "not found" in msg or "404" in msg:
        return HTTPException(status_code=404, detail="Video not found. Check the link and try again.")
    if "timed out" in msg or "timeout" in msg:
        return HTTPException(status_code=504, detail="TikTok took too long to respond. Try again in a bit.")
    # Anything else: TikTok probably changed something or blocked our server IP.
    return HTTPException(
        status_code=502,
        detail="Couldn't fetch this video right now (TikTok may be blocking the request). Try again later.",
    )


def base_ydl_opts() -> dict:
    """Options shared by info + download calls."""
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,          # never download a whole playlist by accident
        "socket_timeout": 25,
        "retries": 2,
        "user_agent": BROWSER_UA,
        # Ask for the watermark-free rendition. yt-dlp's TikTok extractor
        # picks the non-watermarked play URL when available.
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/info")
def video_info(url: str, request: Request) -> JSONResponse:
    """
    Return metadata for a TikTok link: title, author, thumbnail, duration.
    No file is downloaded here.
    """
    check_rate_limit(client_ip(request))
    url = validate_tiktok_url(url)

    opts = base_ydl_opts()
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 — mapped to friendly errors below
        raise friendly_download_error(exc)

    if not info:
        raise HTTPException(status_code=404, detail="Video not found. Check the link and try again.")

    # Guard: refuse absurdly large files early (before /api/download even runs).
    size = info.get("filesize") or info.get("filesize_approx") or 0
    if size and size > MAX_FILESIZE_BYTES:
        raise HTTPException(status_code=413, detail="This video is larger than 100 MB — too big to process.")

    return JSONResponse(
        {
            "id": info.get("id"),
            "title": info.get("title") or "TikTok video",
            "author": info.get("uploader") or info.get("creator") or "unknown",
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),  # seconds, may be None
        }
    )


# Quality presets for /api/download?quality=...
# Keys are what the frontend sends; values are yt-dlp format selectors.
# Each falls back to "best" so a missing exact height never 404s.
QUALITY_FORMATS = {
    "hd": "best[ext=mp4]/best",
    "360": "best[height<=360][ext=mp4]/best[height<=360]/best",
    "240": "best[height<=240][ext=mp4]/best[height<=240]/best",
    "144": "best[height<=144][ext=mp4]/best[height<=144]/best",
}


# Target heights for server-side downscale (TikTok only serves one rendition,
# so lower qualities are transcoded with ffmpeg after download).
QUALITY_HEIGHTS = {"360": 360, "240": 240, "144": 144}


@app.get("/api/download")
def download_video(url: str, request: Request, quality: str = "hd"):
    """
    Download the video (watermark-free) mp4 at the requested quality
    and stream it back. The temp file is deleted after the response is sent.
    """
    check_rate_limit(client_ip(request))
    url = validate_tiktok_url(url)
    if quality not in QUALITY_FORMATS:
        raise HTTPException(status_code=400, detail="Invalid quality. Choose: hd, 360, 240, 144.")

    tmpdir = tempfile.mkdtemp(prefix="ttdl_")

    def cleanup() -> None:
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        opts = base_ydl_opts()
        opts["format"] = QUALITY_FORMATS[quality]  # override the default "best"
        opts["outtmpl"] = str(Path(tmpdir) / "%(id)s.%(ext)s")
        opts["max_filesize"] = MAX_FILESIZE_BYTES  # abort if bigger than ~100MB
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = Path(ydl.prepare_filename(info))

        if not filepath.exists():
            # Fallback: whatever single file landed in the temp dir.
            files = [p for p in Path(tmpdir).iterdir() if p.is_file()]
            if not files:
                raise HTTPException(status_code=502, detail="Download finished but no file was produced.")
            filepath = files[0]

        if filepath.stat().st_size > MAX_FILESIZE_BYTES:
            raise HTTPException(status_code=413, detail="This video is larger than 100 MB — too big to process.")

        # Lower qualities: transcode down with ffmpeg (fast, small files).
        if quality in QUALITY_HEIGHTS:
            height = QUALITY_HEIGHTS[quality]
            small = filepath.with_name(f"{filepath.stem}_{quality}p.mp4")
            proc = subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-i", str(filepath),
                 "-vf", f"scale=-2:{height}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "64k",
                 str(small)],
                capture_output=True, timeout=300,
            )
            if proc.returncode != 0 or not small.exists():
                raise HTTPException(status_code=502, detail="Could not convert to the requested quality. Try HD.")
            filepath.unlink(missing_ok=True)
            filepath = small

        video_id = (info or {}).get("id", "video")
        filename = f"tiktok_{video_id}_{quality}p.mp4" if quality != "hd" else f"tiktok_{video_id}_hd.mp4"
        # BackgroundTask runs after the file has been sent, then deletes it.
        return FileResponse(
            path=str(filepath),
            media_type="video/mp4",
            filename=filename,
            background=BackgroundTask(cleanup),
        )
    except HTTPException:
        cleanup()
        raise
    except Exception as exc:  # noqa: BLE001
        cleanup()
        raise friendly_download_error(exc)


# ---------------------------------------------------------------------------
# Frontend (served from ../frontend so one process hosts site + API)
# ---------------------------------------------------------------------------

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    # Registered AFTER the /api routes so they always win.
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
