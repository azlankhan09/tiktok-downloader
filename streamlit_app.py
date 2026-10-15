"""
TikTok Downloader — Streamlit Cloud edition (free hosting, no card).

Same features as the FastAPI backend:
    paste link -> video preview -> download in HD / 360p / 240p / 144p (no watermark)

Deploy: push this repo to GitHub, then on share.streamlit.io create a new app
with main file "streamlit_app.py". Dependencies come from requirements.txt
at the repo root. ffmpeg is provided by the imageio-ffmpeg pip package
(a static binary — no apt needed), which powers the 360p/240p/144p transcodes.

Made by Azlan Khan.
"""

import subprocess
import tempfile
import urllib.parse
from pathlib import Path

import imageio_ffmpeg
import requests
import streamlit as st
import yt_dlp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ALLOWED_TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
}

# URL shorteners people share TikTok links through (e.g. tt.site).
# We resolve them server-side and only proceed if they land on a real
# tiktok.com video URL — anything else is still rejected.
SHORTENER_HOSTS = {
    "tt.site",
    "www.tt.site",
}

MAX_FILESIZE_BYTES = 100 * 1024 * 1024  # ~100 MB per video

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Quality presets. TikTok serves one rendition, so lower qualities are
# transcoded with ffmpeg after download (same as the FastAPI backend).
QUALITY_FORMATS = {
    "hd": "best[ext=mp4]/best",
    "360": "best[height<=360][ext=mp4]/best[height<=360]/best",
    "240": "best[height<=240][ext=mp4]/best[height<=240]/best",
    "144": "best[height<=144][ext=mp4]/best[height<=144]/best",
}
QUALITY_HEIGHTS = {"360": 360, "240": 240, "144": 144}
QUALITY_LABELS = {"hd": "HD (best quality)", "360": "360p", "240": "240p", "144": "144p"}

WHATSAPP_NUMBER = "923474421152"  # +92 3474421152


# ---------------------------------------------------------------------------
# Helpers (same logic as backend/main.py)
# ---------------------------------------------------------------------------

def resolve_short_url(raw: str) -> str:
    """Follow redirects for known shorteners; return the final URL."""
    try:
        resp = requests.head(
            raw, allow_redirects=True, timeout=15,
            headers={"User-Agent": BROWSER_UA},
        )
        final = resp.url
        if not final or final.rstrip("/") == raw.rstrip("/"):
            # Some shorteners don't answer HEAD properly — try GET.
            resp = requests.get(
                raw, allow_redirects=True, timeout=15,
                headers={"User-Agent": BROWSER_UA}, stream=True,
            )
            final = resp.url
            resp.close()
        if not final:
            raise ValueError("That short link didn't lead anywhere.")
        return final
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not open that short link: {exc}")


def validate_tiktok_url(raw: str, _depth: int = 0) -> str:
    """Accept only https TikTok video/share links. Returns cleaned URL or raises."""
    if not raw or len(raw) > 2048:
        raise ValueError("Please paste a TikTok video link.")
    parsed = urllib.parse.urlparse(raw.strip())
    if parsed.scheme != "https":
        raise ValueError("Link must start with https://")
    host = parsed.netloc.lower().split(":")[0]
    if (host in SHORTENER_HOSTS or host.endswith(".tt.site")) and _depth == 0:
        # Short link (e.g. tt.site) — resolve it, then re-validate the target.
        resolved = resolve_short_url(parsed.geturl())
        return validate_tiktok_url(resolved, _depth=1)
    if host not in ALLOWED_TIKTOK_HOSTS and not host.endswith(".tiktok.com"):
        raise ValueError(
            "Only TikTok links are supported "
            "(tiktok.com, vt.tiktok.com, vm.tiktok.com, tt.site)."
        )
    if not parsed.path or parsed.path == "/":
        raise ValueError("That doesn't look like a video link.")
    return parsed.geturl()


def friendly_download_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "private" in msg:
        return "This video is private or was deleted."
    if "login" in msg or "sign in" in msg:
        return "TikTok requires login for this video — can't download it."
    if "not available" in msg or "not found" in msg or "404" in msg:
        return "Video not found. Check the link and try again."
    if "timed out" in msg or "timeout" in msg:
        return "TikTok took too long to respond. Try again in a bit."
    return "Couldn't fetch this video right now (TikTok may be blocking the request). Try again later."


def base_ydl_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
        "user_agent": BROWSER_UA,
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
    }


def fetch_info(url: str) -> dict:
    opts = base_ydl_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError("Video not found. Check the link and try again.")
    size = info.get("filesize") or info.get("filesize_approx") or 0
    if size and size > MAX_FILESIZE_BYTES:
        raise ValueError("This video is larger than 100 MB — too big to process.")
    return {
        "id": info.get("id") or "video",
        "title": info.get("title") or "TikTok video",
        "author": info.get("uploader") or info.get("creator") or "unknown",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
    }


def download_video(url: str, quality: str) -> tuple[bytes, str]:
    """Download + optionally transcode. Returns (mp4 bytes, filename)."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    tmpdir = tempfile.mkdtemp(prefix="ttdl_")
    try:
        opts = base_ydl_opts()
        opts["format"] = QUALITY_FORMATS[quality]
        opts["outtmpl"] = str(Path(tmpdir) / "%(id)s.%(ext)s")
        opts["max_filesize"] = MAX_FILESIZE_BYTES
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = Path(ydl.prepare_filename(info))

        if not filepath.exists():
            files = [p for p in Path(tmpdir).iterdir() if p.is_file()]
            if not files:
                raise ValueError("Download finished but no file was produced.")
            filepath = files[0]

        if filepath.stat().st_size > MAX_FILESIZE_BYTES:
            raise ValueError("This video is larger than 100 MB — too big to process.")

        if quality in QUALITY_HEIGHTS:
            height = QUALITY_HEIGHTS[quality]
            small = filepath.with_name(f"{filepath.stem}_{quality}p.mp4")
            proc = subprocess.run(
                [ffmpeg, "-y", "-v", "error",
                 "-i", str(filepath),
                 "-vf", f"scale=-2:{height}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "64k",
                 str(small)],
                capture_output=True, timeout=600,
            )
            if proc.returncode != 0 or not small.exists():
                raise ValueError("Could not convert to the requested quality. Try HD.")
            filepath = small

        video_id = (info or {}).get("id", "video")
        filename = f"tiktok_{video_id}_{quality}.mp4"
        return filepath.read_bytes(), filename
    finally:
        # Clean the temp dir (bytes are already in memory).
        for p in Path(tmpdir).glob("*"):
            p.unlink(missing_ok=True)
        Path(tmpdir).rmdir()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="TikTok Downloader", page_icon="⬇️", layout="centered")

st.title("⬇️ TikTok Downloader")
st.caption("Download public TikTok videos without watermark — free, no sign-up needed.")

# Session state: keeps preview + file across reruns so nothing re-downloads.
for key, default in (("meta", None), ("video_bytes", None), ("video_name", None), ("error", None)):
    st.session_state.setdefault(key, default)

url = st.text_input("Paste a TikTok video link:", placeholder="https://www.tiktok.com/@user/video/123...")

col1, col2 = st.columns([1, 1])
with col1:
    get_info = st.button("🔍 Get Video", use_container_width=True)
with col2:
    if st.button("🧹 Clear", use_container_width=True):
        st.session_state.meta = None
        st.session_state.video_bytes = None
        st.session_state.video_name = None
        st.session_state.error = None
        st.rerun()

if get_info:
    st.session_state.meta = None
    st.session_state.video_bytes = None
    st.session_state.video_name = None
    st.session_state.error = None
    try:
        clean = validate_tiktok_url(url)
        with st.spinner("Fetching video info..."):
            st.session_state.meta = fetch_info(clean)
            st.session_state.clean_url = clean
    except Exception as exc:  # noqa: BLE001 — shown to the user as a friendly message
        st.session_state.error = friendly_download_error(exc) if not isinstance(exc, ValueError) else str(exc)

if st.session_state.error:
    st.error(st.session_state.error)

meta = st.session_state.meta
if meta:
    st.divider()
    c1, c2 = st.columns([1, 2])
    with c1:
        if meta["thumbnail"]:
            st.image(meta["thumbnail"], use_container_width=True)
    with c2:
        st.subheader(meta["title"])
        st.write(f"👤 **{meta['author']}**")
        if meta["duration"]:
            st.write(f"⏱️ {int(meta['duration'])} seconds")

    quality = st.radio(
        "Choose quality:",
        options=["hd", "360", "240", "144"],
        format_func=lambda q: QUALITY_LABELS[q],
        horizontal=True,
    )

    if st.button("⬇️ Download Video", type="primary", use_container_width=True):
        st.session_state.video_bytes = None
        st.session_state.video_name = None
        st.session_state.error = None
        try:
            with st.spinner("Downloading... (lower qualities take a little longer)"):
                data, name = download_video(st.session_state.clean_url, quality)
            st.session_state.video_bytes = data
            st.session_state.video_name = name
            st.success(f"Done! ({len(data) / 1024 / 1024:.1f} MB)")
        except Exception as exc:  # noqa: BLE001
            st.session_state.error = friendly_download_error(exc) if not isinstance(exc, ValueError) else str(exc)
            st.rerun()

    if st.session_state.video_bytes:
        st.download_button(
            label="💾 Save MP4 to your device",
            data=st.session_state.video_bytes,
            file_name=st.session_state.video_name,
            mime="video/mp4",
            use_container_width=True,
        )

st.divider()
st.markdown(
    """
    <div style="text-align:center; color:gray; font-size:0.9em;">
        Made by <b>Azlan Khan</b><br>
        Want any website made? Contact me!<br>
        📱 WhatsApp: <a href="https://wa.me/923474421152">+92 3474421152</a>
    </div>
    """,
    unsafe_allow_html=True,
)
