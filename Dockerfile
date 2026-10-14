# Render (free tier) Docker deploy for the TikTok downloader site.
# Includes ffmpeg (needed for the 360p/240p/144p quality options).
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copies backend/ + frontend/ (backend serves the frontend itself).
COPY . .

EXPOSE 8000

# Render sets $PORT; default 8000 for local docker runs.
CMD ["sh", "-c", "cd backend && uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
