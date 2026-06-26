# ============================================================
# Stage 1 — builder
# Install Python dependencies into an isolated layer.
# This stage is never shipped — only its /install output is.
# ============================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools needed to compile any C-extension packages.
# Cleaned up in the same RUN layer to avoid bloating the image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install \
      --no-cache-dir \
      --prefix=/install \
      -r requirements.txt


# ============================================================
# Stage 2 — runtime
# Lean final image: Python + ffmpeg + yt-dlp + our app code.
# ============================================================
FROM python:3.12-slim AS runtime

# --- labels ---
LABEL maintainer="contact@johnlouiecleofas.com" \
      description="yt-mp3: YouTube to MP3 converter" \
      version="1.0.0"

# --- system deps ---
# ffmpeg: required by yt-dlp to re-encode audio to MP3.
# curl:   used by the HEALTHCHECK below.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

ENV FFMPEG_LOCATION=/usr/bin/ffmpeg

# --- yt-dlp ---
# Installed via pip rather than the standalone binary so it sits
# on PATH the same way in every environment.
RUN pip install --no-cache-dir yt-dlp

# --- copy installed Python packages from builder ---
COPY --from=builder /install /usr/local

# --- non-root user ---
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --no-create-home appuser

# --- app directory ---
WORKDIR /app

COPY app/ .

# Temp dir for conversions — owned by appuser so writes succeed.
RUN mkdir -p /tmp/yt-mp3 && chown appuser:appgroup /tmp/yt-mp3

USER appuser

# --- healthcheck ---
# Docker will mark the container unhealthy if this fails 3 times.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8000/api/docs > /dev/null 2>&1 || exit 1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--no-access-log"]