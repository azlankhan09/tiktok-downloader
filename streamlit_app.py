"""
TikTok Downloader — Streamlit Cloud edition (free hosting, no card).

Paste link -> video preview -> download in HD / 360p / 240p / 144p (no watermark)

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
QUALITY_LABELS = {"hd": "HD", "360": "360p", "240": "240p", "144": "144p"}
QUALITY_ORDER = ["hd", "360", "240", "144"]

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
# Professional UI
# ---------------------------------------------------------------------------

st.set_page_config(page_title="TikTok Downloader", page_icon="⬇️", layout="centered")

PRO_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700;800&display=swap');

/* ---- hide Streamlit chrome for an app-like feel ---- */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header[data-testid="stHeader"] {visibility: hidden;}

/* ---- app canvas ---- */
.stApp {
    background:
        radial-gradient(900px 480px at 50% -8%, rgba(254,44,85,.14) 0%, transparent 60%),
        radial-gradient(700px 420px at 85% 110%, rgba(37,244,238,.08) 0%, transparent 60%),
        #0c0c11;
}
.block-container {
    max-width: 620px;
    padding-top: 2.2rem;
    padding-bottom: 2rem;
    font-family: 'Plus Jakarta Sans', 'Segoe UI', system-ui, sans-serif;
}

/* ---- hero ---- */
.hero { text-align: center; margin-bottom: 1.6rem; }
.app-icon {
    width: 76px; height: 76px; margin: 0 auto 1rem;
    border-radius: 24px;
    background: linear-gradient(135deg, #FE2C55 0%, #7c3aed 100%);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 10px 34px rgba(254,44,85,.38);
}
.hero-title {
    color: #ffffff; font-size: 2.15rem; font-weight: 800;
    margin: 0; letter-spacing: -0.6px; line-height: 1.15;
}
.grad-text {
    background: linear-gradient(92deg, #FE2C55 10%, #a855f7 55%, #25F4EE 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
}
.hero-sub { color: #a3a3ad; margin: .65rem 0 1.05rem; font-size: .95rem; line-height: 1.55; }
.badges { display: flex; gap: .5rem; justify-content: center; flex-wrap: wrap; }
.badge {
    font-size: .72rem; font-weight: 700; letter-spacing: .3px; color: #e6e6eb;
    background: rgba(255,255,255,.055); border: 1px solid rgba(255,255,255,.11);
    padding: .32rem .75rem; border-radius: 999px;
}
.badge .dot { color: #25F4EE; margin-right: .3rem; }

/* ---- input card ---- */
.card {
    background: rgba(255,255,255,.035);
    border: 1px solid rgba(255,255,255,.09);
    border-radius: 20px;
    padding: 1.15rem 1.15rem 1.25rem;
    box-shadow: 0 12px 40px rgba(0,0,0,.35);
    margin-bottom: 1rem;
}
.card-label {
    display: block; color: #e6e6eb; font-weight: 700; font-size: .88rem;
    margin-bottom: .55rem;
}

/* ---- text input ---- */
.stTextInput > div > div > input {
    background: rgba(0,0,0,.35) !important;
    border: 1px solid rgba(255,255,255,.13) !important;
    border-radius: 14px !important;
    color: #fff !important;
    padding: .78rem 1rem !important;
    font-size: .93rem !important;
}
.stTextInput > div > div > input::placeholder { color: #6b6b75 !important; }
.stTextInput > div > div > input:focus {
    border-color: #FE2C55 !important;
    box-shadow: 0 0 0 3px rgba(254,44,85,.22) !important;
}

/* ---- buttons ---- */
button[data-testid="stBaseButton-primary"] {
    background: linear-gradient(135deg, #FE2C55 0%, #b026ff 100%) !important;
    color: #fff !important; border: none !important;
    border-radius: 14px !important; font-weight: 700 !important;
    padding: .72rem 1rem !important; letter-spacing: .2px !important;
    box-shadow: 0 8px 26px rgba(254,44,85,.35) !important;
    transition: filter .15s ease, transform .15s ease !important;
}
button[data-testid="stBaseButton-primary"]:hover { filter: brightness(1.12); transform: translateY(-1px); }
button[data-testid="stBaseButton-secondary"] {
    background: rgba(255,255,255,.05) !important;
    color: #e6e6eb !important;
    border: 1px solid rgba(255,255,255,.13) !important;
    border-radius: 14px !important; font-weight: 600 !important;
    padding: .72rem 1rem !important;
    transition: background .15s ease !important;
}
button[data-testid="stBaseButton-secondary"]:hover { background: rgba(255,255,255,.1) !important; }

/* ---- video preview card ---- */
.video-card {
    display: flex; gap: 1rem; align-items: flex-start;
    background: rgba(255,255,255,.035);
    border: 1px solid rgba(255,255,255,.09);
    border-radius: 20px; padding: 1rem;
    box-shadow: 0 12px 40px rgba(0,0,0,.35);
    margin: 1.1rem 0 1rem;
}
.video-card img.thumb {
    width: 128px; height: 170px; object-fit: cover; border-radius: 14px;
    box-shadow: 0 6px 20px rgba(0,0,0,.5); flex-shrink: 0;
    background: #1a1a22;
}
.vtitle { color: #fff; font-weight: 700; font-size: .98rem; line-height: 1.4; margin-bottom: .6rem;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.chips { display: flex; gap: .45rem; flex-wrap: wrap; }
.chip {
    font-size: .74rem; font-weight: 600; color: #d8d8e0;
    background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.1);
    padding: .28rem .65rem; border-radius: 999px;
}
.chip.hot { color: #ffd9e1; border-color: rgba(254,44,85,.45); background: rgba(254,44,85,.12); }

/* ---- quality pills (radio restyled) ---- */
div[data-testid="stRadio"] > div[role="radiogroup"] { gap: .5rem; flex-wrap: wrap; }
div[data-testid="stRadio"] label[data-testid="stWidgetLabel"] p { color: #e6e6eb; font-weight: 700; font-size: .88rem; }
div[data-testid="stRadio"] div[role="radiogroup"] > label {
    background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.13);
    border-radius: 999px; padding: .42rem .95rem;
    transition: all .15s ease;
}
div[data-testid="stRadio"] div[role="radiogroup"] > label p { color: #d8d8e0; font-weight: 600; font-size: .85rem; margin: 0; }
div[data-testid="stRadio"] div[role="radiogroup"] > label > div:first-child { display: none; }
div[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) {
    background: linear-gradient(135deg, #FE2C55, #b026ff);
    border-color: transparent;
    box-shadow: 0 4px 16px rgba(254,44,85,.35);
}
div[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) p { color: #fff; }

/* ---- divider glow ---- */
hr {
    border: none; height: 1px; margin: 1.4rem 0;
    background: linear-gradient(90deg, transparent, rgba(254,44,85,.45), rgba(37,244,238,.25), transparent);
}

/* ---- footer ---- */
.footer { text-align: center; padding: .4rem 0 1rem; }
.made { color: #8f8f99; font-size: .88rem; margin: 0 0 .25rem; }
.made b {
    background: linear-gradient(92deg, #FE2C55, #a855f7);
    -webkit-background-clip: text; background-clip: text; color: transparent;
    font-weight: 800;
}
.cta { color: #b9b9c2; font-size: .85rem; margin: 0 0 .8rem; }
.wa-btn {
    display: inline-block; text-decoration: none !important;
    color: #4ade80 !important; font-weight: 700; font-size: .88rem;
    border: 1px solid rgba(74,222,128,.4); background: rgba(74,222,128,.08);
    padding: .55rem 1.3rem; border-radius: 999px;
    transition: background .15s ease;
}
.wa-btn:hover { background: rgba(74,222,128,.16); }

/* ---- alerts / spinner on dark ---- */
.stAlert { border-radius: 14px; }
div[data-testid="stSpinner"] { color: #e6e6eb; }
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
"""

st.markdown(PRO_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Hero
# ---------------------------------------------------------------------------
st.markdown(
    """
    <div class="hero">
        <div class="app-icon">
            <svg width="38" height="38" viewBox="0 0 24 24" fill="none">
                <path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 19h16"
                      stroke="white" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
        </div>
        <h1 class="hero-title">TikTok <span class="grad-text">Downloader</span></h1>
        <p class="hero-sub">Download public TikTok videos without watermark.<br>Free forever &mdash; no sign-up needed.</p>
        <div class="badges">
            <span class="badge"><span class="dot">&#9679;</span>100% Free</span>
            <span class="badge"><span class="dot">&#9679;</span>No Watermark</span>
            <span class="badge"><span class="dot">&#9679;</span>HD Quality</span>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# State + input card
# ---------------------------------------------------------------------------
for key, default in (("meta", None), ("video_bytes", None), ("video_name", None), ("error", None)):
    st.session_state.setdefault(key, default)

st.markdown('<div class="card"><span class="card-label">Paste a TikTok video link</span>',
            unsafe_allow_html=True)
url = st.text_input(
    "Paste a TikTok video link",
    placeholder="https://www.tiktok.com/@user/video/123...",
    label_visibility="collapsed",
)
col1, col2 = st.columns([1, 1])
with col1:
    get_info = st.button("Get Video", type="primary", use_container_width=True)
with col2:
    if st.button("Clear", use_container_width=True):
        st.session_state.meta = None
        st.session_state.video_bytes = None
        st.session_state.video_name = None
        st.session_state.error = None
        st.rerun()
st.markdown("</div>", unsafe_allow_html=True)

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

# ---------------------------------------------------------------------------
# Video preview + download
# ---------------------------------------------------------------------------
meta = st.session_state.meta
if meta:
    import html as _html

    title = _html.escape(meta["title"] or "TikTok video")
    author = _html.escape(meta["author"] or "unknown")
    thumb = _html.escape(meta["thumbnail"] or "", quote=True)
    dur_html = ""
    if meta["duration"]:
        dur_html = f'<span class="chip">&#9201; {int(meta["duration"])}s</span>'
    st.markdown(
        f"""
        <div class="video-card">
            <img class="thumb" src="{thumb}" alt="video thumbnail">
            <div>
                <div class="vtitle">{title}</div>
                <div class="chips">
                    <span class="chip hot">@ {author}</span>
                    {dur_html}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    quality = st.radio(
        "Choose quality",
        options=QUALITY_ORDER,
        format_func=lambda q: QUALITY_LABELS[q],
        horizontal=True,
        index=0,
    )

    if st.button("Download Video", type="primary", use_container_width=True):
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
            label="Save MP4 to your device",
            data=st.session_state.video_bytes,
            file_name=st.session_state.video_name,
            mime="video/mp4",
            type="primary",
            use_container_width=True,
        )

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.markdown(
    """
    <hr>
    <div class="footer">
        <p class="made">Made by <b>Azlan Khan</b></p>
        <p class="cta">Want your own website? Contact me!</p>
        <a class="wa-btn" href="https://wa.me/923474421152" target="_blank">WhatsApp: +92 3474421152</a>
    </div>
    """,
    unsafe_allow_html=True,
)
