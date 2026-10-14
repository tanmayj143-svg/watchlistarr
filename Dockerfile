# Watchlistarr — ARR-ecosystem watchlist request bot
#
# Slim single-stage build: the app is pure Python with a handful of small
# wheels, so there is nothing to compile and nothing to save by going
# multi-stage.
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Watchlistarr" \
      org.opencontainers.image.description="Letterboxd/IMDb watchlists -> scored Seerr requests" \
      org.opencontainers.image.version="1.7.3" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WATCHLISTARR_DATA=/data \
    WATCHLISTARR_PORT=7777 \
    WATCHLISTARR_HOST=0.0.0.0

WORKDIR /app

# curl is used by the compose healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run unprivileged; /data is a volume so the UID must own it.
RUN useradd --system --uid 10001 --create-home watchlistarr \
 && mkdir -p /data \
 && chown -R watchlistarr:watchlistarr /app /data
USER watchlistarr

VOLUME ["/data"]
EXPOSE 7777

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${WATCHLISTARR_PORT}/healthz" || exit 1

CMD ["python", "-m", "app"]