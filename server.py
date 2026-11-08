#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import base64
import html as htmllib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------------- config ----------------
PORT = int(os.environ.get("PORT", "8000"))
API_KEY = os.environ.get("DOWNLOADER_API_KEY", "")
YTDLP = os.environ.get("YTDLP_BIN", "yt-dlp")
# Optional, OFF by default (owner decides):
#   PROXY_URL       e.g. http://user:pass@host:port  (residential proxy)
#   IG_COOKIES_B64  base64 of a Netscape cookies.txt from a THROWAWAY
#                   Instagram account. Lives only in the server env.
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
IG_COOKIES_B64 = os.environ.get("IG_COOKIES_B64", "").strip()
COOKIES_FILE = ""
CACHE_TTL = 600
MAX_PARSE_CONCURRENT = 3
RATE_LIMIT = 20          # requests per minute per IP
RATE_WINDOW = 60

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "youtu.be", "music.youtube.com",
    "m.youtube.com", "tiktok.com", "www.tiktok.com",
    "vm.tiktok.com", "vt.tiktok.com",
    "facebook.com", "www.facebook.com", "fb.watch",
    "instagram.com", "www.instagram.com",
}

BANNER = r"""
 ____   _____        _    _                 _
|  _ \ |  _  \  ___ | | _| | ___   __ _  __| | ___ _ __
| | | | | | | |/ _ \| |/ / |/ _ \ / _` |/ _` |/ _ \ '__|
| |_| | | |_| | (_) |   <| | (_) | (_| | (_| |  __/ |
|____/  |____/ \___/|_|\_\_|\___/ \__,_|\__,_|\___|_|
            Downloader Server v1.0
"""

# ---------------- state ----------------
_cache = {}
_cache_lock = threading.Lock()
_parse_sem = threading.Semaphore(MAX_PARSE_CONCURRENT)
_hits = {}
_hits_lock = threading.Lock()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def platform_of(host):
    if "youtu" in host:
        return "youtube"
    if "tiktok" in host:
        return "tiktok"
    if "facebook" in host or "fb.watch" in host:
        return "facebook"
    if "instagram" in host:
        return "instagram"
    return "unknown"


def rate_ok(ip):
    now = time.time()
    with _hits_lock:
        lst = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
        if len(lst) >= RATE_LIMIT:
            _hits[ip] = lst
            return False
        lst.append(now)
        _hits[ip] = lst
        return True


def cache_get(url):
    with _cache_lock:
        item = _cache.get(url)
        if item and time.time() - item[0] < CACHE_TTL:
            return item[1]
        if item:
            del _cache[url]
        return None


def cache_put(url, result):
    with _cache_lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[url] = (time.time(), result)


def pick_formats(info):
    muxed, video_only, audio_only = [], [], []
    for f in info.get("formats") or []:
        url = f.get("url")
        if not url or not url.startswith("http"):
            continue
        if f.get("protocol") not in (None, "https", "http"):
            continue
        ext = f.get("ext") or "mp4"
        height = f.get("height") or 0
        size = f.get("filesize") or f.get("filesize_approx") or 0
        vc, ac = f.get("vcodec"), f.get("acodec")
        item = {
            "id": str(f.get("format_id")),
            "ext": ext,
            "height": height,
            "size": size,
            "url": url,
        }
        if vc not in (None, "none") and ac not in (None, "none"):
            item["kind"] = "video"
            item["label"] = f"{height}p" if height else ext.upper()
            muxed.append(item)
        elif vc not in (None, "none"):
            item["kind"] = "video"
            item["label"] = f"{height}p" if height else ext.upper()
            video_only.append(item)
        elif ac not in (None, "none"):
            item["kind"] = "audio"
            abr = f.get("abr") or 0
            item["label"] = f"MP3 {int(abr)}kbps" if abr else "MP3"
            audio_only.append(item)
    muxed.sort(key=lambda x: -x["height"])
    video_only.sort(key=lambda x: -x["height"])
    audio_only.sort(key=lambda x: -x["size"])
    return muxed, video_only, audio_only


def setup_cookies():
    global COOKIES_FILE
    if not IG_COOKIES_B64:
        return
    try:
        path = os.path.join(tempfile.gettempdir(), "ig_cookies.txt")
        with open(path, "wb") as f:
            f.write(base64.b64decode(IG_COOKIES_B64))
        COOKIES_FILE = path
        log("instagram cookies loaded (server-side only)")
    except Exception as e:
        log(f"cookies setup failed: {e}")


def run_ytdlp(url, use_cookies):
    cmd = [YTDLP, "-J", "--no-playlist", "--no-warnings",
           "--socket-timeout", "20"]
    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]
    if use_cookies and COOKIES_FILE:
        tmp = COOKIES_FILE + ".run"
        try:
            with open(COOKIES_FILE, "rb") as a, open(tmp, "wb") as b:
                b.write(a.read())
            cmd += ["--cookies", tmp]
        except Exception:
            pass
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=90)


_IG_UAS = [
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
    "Mobile/15E148 Safari/604.1",
]


def _deep_unescape(t):
    for _ in range(4):
        t = htmllib.unescape(t)
        t = (t.replace('\\"', '"').replace("\\/", "/")
              .replace("\\u0026", "&").replace("\\\\", "\\"))
    return t


def ig_embed_fallback(url):
    """Own-code fallback: read video_url from the public embed page.
    Same server IP as yt-dlp, so it only helps when yt-dlp's own
    extractor chain is the thing that broke (not a pure IP block)."""
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    sc = m.group(1)
    handler = urllib.request.ProxyHandler(
        {"http": PROXY_URL, "https": PROXY_URL}) if PROXY_URL else urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(handler)
    for ua in _IG_UAS:
        for path in (f"reel/{sc}/embed/captioned/", f"p/{sc}/embed/captioned/"):
            try:
                req = urllib.request.Request(
                    f"https://www.instagram.com/{path}",
                    headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"})
                with opener.open(req, timeout=20) as r:
                    text = _deep_unescape(r.read().decode("utf-8", "ignore"))
            except Exception as e:
                log(f"embed fallback {path}: {e}")
                continue
            v = re.search(r'"video_url":"(https?://[^"]+)"', text)
            if not v:
                continue
            vurl = v.group(1).replace("\\", "")
            thumb = re.search(r'"display_url":"(https?://[^"]+)"', text)
            user = re.search(r'"username":"([^"]+)"', text)
            return {
                "ok": True, "platform": "instagram", "id": sc,
                "title": None,
                "thumbnail": thumb.group(1).replace("\\", "") if thumb else None,
                "duration": None,
                "uploader": user.group(1) if user else None,
                "bestUrl": vurl, "bestLabel": "MP4",
                "formats": [{"id": "embed", "ext": "mp4", "height": 0,
                             "size": 0, "url": vurl,
                             "kind": "video", "label": "MP4"}],
            }
    return None


def parse_url(url):
    cached = cache_get(url)
    if cached is not None:
        return cached
    is_ig = "instagram.com" in url
    with _parse_sem:
        try:
            p = run_ytdlp(url, use_cookies=False)
            # Anonymous failed on Instagram -> retry with the server-side
            # session if the owner configured one.
            if p.returncode != 0 and is_ig and COOKIES_FILE:
                log("anonymous failed, retrying with server cookies")
                p = run_ytdlp(url, use_cookies=True)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "TIMEOUT",
                    "message": "انتهت المهلة، عاود المحاولة"}
        except FileNotFoundError:
            return {"ok": False, "error": "SERVER_ERROR",
                    "message": "yt-dlp غير مثبت على السيرفر"}
    if p.returncode != 0:
        raw = (p.stderr or "").strip()
        log(f"yt-dlp FAIL: {raw[-400:]}")
        if is_ig:
            fb = ig_embed_fallback(url)
            if fb:
                log("embed fallback OK")
                cache_put(url, fb)
                return fb
        err = raw.lower()
        if "unsupported url" in err or "no video formats" in err:
            code, msg = "UNSUPPORTED", "هاد الرابط غير مدعوم"
        elif ("rate-limit" in err or "login required" in err
              or "empty media response" in err or "log in" in err):
            code, msg = "IG_BLOCKED", "إنستغرام كيحبس السيرفر، عاود من بعد"
        elif "private" in err:
            code, msg = "PRIVATE", "هاد الفيديو خاص، مايمكنش تحميلو"
        elif "not found" in err or "404" in err or "removed" in err:
            code, msg = "NOT_FOUND", "الفيديو ماموجودش أو تمسح"
        else:
            code, msg = "PARSE_FAILED", "ماقدرتش نقرا هاد الفيديو"
        return {"ok": False, "error": code, "message": msg}
    try:
        info = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "PARSE_FAILED",
                "message": "ماقدرتش نقرا هاد الفيديو"}
    if info.get("entries"):
        info = info["entries"][0]
    muxed, video_only, audio_only = pick_formats(info)
    if not muxed and not video_only:
        return {"ok": False, "error": "NO_FORMATS",
                "message": "مالقيتش حتى جودة للتحميل"}
    best = (muxed or video_only)[0]
    result = {
        "ok": True,
        "platform": platform_of(urlparse(url).netloc.lower()),
        "id": info.get("id"),
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "bestUrl": best["url"],
        "bestLabel": best["label"],
        "formats": muxed[:6] + video_only[:4] + audio_only[:2],
    }
    cache_put(url, result)
    return result


# ---------------- http ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        if not API_KEY:
            return True
        return self.headers.get("X-API-Key") == API_KEY

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            return self._send(200, {"ok": True, "version": "1.1"})
        return self._send(404, {"ok": False, "error": "NOT_FOUND"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/parse":
            return self._send(404, {"ok": False, "error": "NOT_FOUND"})
        ip = self.client_address[0]
        if not rate_ok(ip):
            return self._send(429, {"ok": False, "error": "RATE_LIMIT",
                                    "message": "بزاف ديال الطلبات، تسنى شوية"})
        if not self._auth_ok():
            return self._send(401, {"ok": False, "error": "UNAUTHORIZED"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"ok": False, "error": "BAD_REQUEST"})
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return self._send(400, {"ok": False, "error": "BAD_URL",
                                    "message": "الرابط غير صحيح"})
        host = urlparse(url).netloc.lower().lstrip("www.")
        if host not in ALLOWED_HOSTS and urlparse(url).netloc.lower() not in ALLOWED_HOSTS:
            return self._send(400, {"ok": False, "error": "UNSUPPORTED",
                                    "message": "هاد الموقع غير مدعوم"})
        log(f"parse {platform_of(host)} {url[:80]}")
        result = parse_url(url)
        self._send(200 if result.get("ok") else 422, result)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(BANNER)
    setup_cookies()
    log(f"listening on 0.0.0.0:{PORT} | yt-dlp: {YTDLP} | "
        f"auth: {'ON' if API_KEY else 'OFF (dev)'} | "
        f"proxy: {'ON' if PROXY_URL else 'off'} | cookies: {'ON' if COOKIES_FILE else 'off'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
