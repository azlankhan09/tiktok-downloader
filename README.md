# TikTok Downloader (no watermark)

A public, no-login website: paste a TikTok video link → get an MP4 without the watermark.

```
tiktok-downloader/
├── backend/      # FastAPI + yt-dlp API (serves the frontend too)
│   ├── main.py
│   ├── requirements.txt
│   └── README.md
├── frontend/     # Single index.html — no build step, mobile-friendly
│   └── index.html
└── README.md     # this file
```

## How it works

```
  Browser ── paste link ──▶ FastAPI (/api/info) ──▶ yt-dlp ──▶ TikTok (unofficial)
        ◀── thumbnail/title preview ──                  (metadata only)
  Browser ── click download ──▶ FastAPI (/api/download) ──▶ yt-dlp ──▶ TikTok CDN
        ◀── MP4 file (watermark-free when TikTok serves it) ──
```

1. **`/api/info?url=`** — validates the link (TikTok hosts only), asks yt-dlp for metadata, returns title/author/thumbnail/duration as JSON. No file is downloaded.
2. **`/api/download?url=`** — yt-dlp downloads the best-quality mp4 into a temp folder, FastAPI streams it to the browser, then deletes the temp file. Videos over ~100 MB are refused; each IP is limited to 10 requests/minute.

## Run it locally

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

`ffmpeg` must be installed (yt-dlp needs it for some merges).

## HONEST limitations — read before going public

1. **No official TikTok API.** TikTok doesn't offer one for this. Everything runs on yt-dlp's unofficial extraction, which breaks whenever TikTok changes its site. Expect to update `yt-dlp` often (`pip install -U yt-dlp`) and expect occasional multi-day outages.
2. **Datacenter IPs get blocked.** TikTok aggressively rate-limits/blocks cloud hosting IPs. The site may work fine from your laptop but fail on Render/Railway/VPS with "couldn't fetch this video" errors. The real fix (residential proxies) costs money — there is no free fix.
3. **Bandwidth costs money.** Every download pulls the full video through *your* server first, then to the user. A few hundred downloads/day = tens of GB/month. Render's free tier (~100 GB/month bandwidth) can handle a small site; a viral spike can suspend it.
4. **ToS gray area.** Downloading videos this way violates TikTok's Terms of Service. For personal use it's widely tolerated, but running it as a public service carries some risk (takedown notices, IP blocks). The footer asks users to respect creators — keep that.
5. **"No watermark" isn't guaranteed.** It works because TikTok's CDN serves a clean rendition that yt-dlp can reach today. If TikTok stops serving it, downloads fall back to watermarked or fail.

## Deploy (free path)

**Render, no card needed:** New → Web Service → build `pip install -r backend/requirements.txt` → start `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`. Free tier sleeps after inactivity (first visit takes ~50s to wake).

See `backend/README.md` for Railway/VPS notes.
