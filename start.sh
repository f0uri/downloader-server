#!/bin/sh
# Instagram changes its pages every few weeks; an old yt-dlp is the
# most common reason reels suddenly stop parsing. Update on every start
# (bounded to 60s, never blocks startup if pip/network fails).
timeout 60 pip install --no-cache-dir -U yt-dlp >/dev/null 2>&1 || true
yt-dlp --version 2>/dev/null | sed 's/^/yt-dlp version: /'
exec python server.py
