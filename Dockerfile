FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -U yt-dlp

WORKDIR /app
COPY server.py start.sh ./
RUN chmod +x start.sh

CMD ["./start.sh"]
