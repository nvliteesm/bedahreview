"""Minimal local web app: paste reviews, get the Fix / Fight / Ignore board. Phone-first, installable (PWA).

    python -m bedahreview.web            # then open http://127.0.0.1:8000

Standard library only. Locally it listens on 127.0.0.1 so the Gemini key (read from .env) never leaves
this machine, and it refuses requests whose Host header is not localhost, which blocks other
websites from using your key through the browser.

Public mode (Cloud Run sets PORT, or set BEDAHREVIEW_PUBLIC=1) listens on all interfaces. It then
refuses to start without BEDAHREVIEW_ACCESS_CODE: visitors sign in with that code, and the number of
analyses per visitor per hour is capped, so a stranger cannot spend the Gemini key.
"""
from __future__ import annotations

import warnings

# Hide the old-Python / LibreSSL notices from Google's libraries (same as `python -W ignore`).
warnings.filterwarnings("ignore")

import argparse
import hashlib
import hmac
import html
import json
import os
import sys
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import parse_qs

from .classifier import ClassificationError, classify, resolve_model
from .env import has_api_key, load_dotenv
from .fetchers import MAX_LIMIT, Fetcher, FetcherConfigError, FetchError, get_fetcher, product_title_from_url, to_review_dicts
from .fetchers.base import check_mode, check_shopee_product_url, clamp_limit
from .parse import parse_reviews

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 200_000
MAX_REVIEWS = 50  # v1 cap: keeps one paste to a handful of API calls
BUCKET_ORDER = ["fix", "fight", "check", "ignore", "keep"]
DEFAULT_LIMIT = 10  # reviews fetched from a link when the seller does not choose
MAX_PRODUCT_CHARS = 150
COOKIE = "br_session"
SESSION_SECS = 30 * 24 * 3600
MIN_ACCESS_CODE_CHARS = 8
MAX_LOGIN_FAILS = 5  # per visitor, per LOGIN_WINDOW_SECS
LOGIN_WINDOW_SECS = 15 * 60
DEFAULT_MAX_PER_HOUR = 30  # analyses per visitor per hour in public mode
PUBLIC_ASSETS = {"/manifest.webmanifest", "/sw.js", "/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png"}
# Files served from static/ (exact paths only; the query string is ignored, so /?text=... is the page).
PAGES = {"/": "index.html", "/index.html": "index.html"}
ASSETS = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
}


class RateLimiter:
    """Allow at most `limit` events per key per `window` seconds. In memory, so it assumes one server instance."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> Deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.time())) >= self.limit

    def add(self, key: str) -> None:
        with self._lock:
            now = time.time()
            self._recent(key, now).append(now)

    def take(self, key: str) -> bool:
        """Count one event; False when the key is already at its limit."""
        with self._lock:
            now = time.time()
            hits = self._recent(key, now)
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def session_token(access_code: str) -> str:
    return hmac.new(access_code.encode("utf-8"), b"bedahreview-session", hashlib.sha256).hexdigest()


def safe_next(value: str) -> str:
    """Only send the visitor back to a path on this site."""
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value and "\r" not in value else "/"


LOGIN_PAGE = """<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BedahReview</title>
<style>
:root{color-scheme:light dark;--bg:#faf8f5;--fg:#1d1b18;--card:#fff;--line:#d9d3ca;--accent:#1f6f5c;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--fg:#f2eee8;--card:#201e1b;--line:#3a3631;--accent:#58b79f;--bad:#f2b8b5}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;display:grid;min-height:100vh;place-items:center;padding:16px}
main{width:100%%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 16px;opacity:.8}
input,button{width:100%%;box-sizing:border-box;font:inherit;padding:12px;border-radius:10px;border:1px solid var(--line)}
input{background:var(--bg);color:var(--fg);margin-bottom:12px}
button{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.err{color:var(--bad);margin:0 0 12px}
</style></head><body><main>
<h1>BedahReview</h1><p>Masukkan kode akses / Enter the access code.</p>
%(error)s
<form method="post" action="/login">
<input type="password" name="code" autocomplete="current-password" autofocus required aria-label="Access code">
<input type="hidden" name="next" value="%(next)s">
<button type="submit">Masuk / Sign in</button>
</form></main></body></html>
"""


def _base_variant(variant: Optional[str]) -> Optional[str]:
    """'Putih, L' -> 'Putih': complaints are grouped by the first part of the variant (usually the colour)."""
    base = (variant or "").split(",")[0].strip()
    return base or None


def fix_groups(items: List[Dict[str, Any]], complaints: int) -> List[Dict[str, Any]]:
    """Group Fix-it reviews into cards by theme and variant, biggest first, each with 'n of complaints'."""
    groups: Dict[Any, List[str]] = {}
    for item in items:
        if item["bucket"] == "fix" and not item["likely_fake"]["flag"]:
            groups.setdefault((item["theme"], _base_variant(item["variant"])), []).append(item["id"])
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))  # stable: ties keep input order
    return [{"theme": t, "variant": v, "ids": ids, "n": len(ids), "of": complaints} for (t, v), ids in ordered]


def build_response(
    reviews: List[Dict[str, Any]], results: List[Dict[str, Any]], model: str, source: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Join each classifier result with the review it came from and add the counts the board shows."""
    by_id = {r["id"]: r for r in reviews}
    items = []
    for result in results:
        review = by_id[result["id"]]
        items.append(dict(result, stars=review["stars"], text=review["text"], variant=result.get("variant") or review["variant"]))
    real = [item for item in items if not item["likely_fake"]["flag"]]  # likely-fake reviews stay out of every count
    counts = Counter(item["bucket"] for item in real)
    complaints = counts.get("fix", 0) + counts.get("fight", 0) + counts.get("check", 0)
    stars = [r["stars"] for r in reviews if r["stars"]]
    return {
        "model": model,
        "items": items,
        "counts": {b: counts.get(b, 0) for b in BUCKET_ORDER},
        "complaints": complaints,
        "groups": fix_groups(items, complaints),
        "likely_fake": len(items) - len(real),
        "no_text": sum(1 for item in items if item["theme"] == "No text"),
        "dropped": len(reviews) - len(items),  # reviews left out because the model's quote could not be verified
        "reviews": len(reviews),
        "star_avg": round(sum(stars) / len(stars), 1) if stars else None,
        "source": source or {"kind": "paste"},
    }


class Handler(BaseHTTPRequestHandler):
    classify_fn: Callable[..., List[Dict[str, Any]]] = staticmethod(classify)  # replaced in tests
    fetcher: Optional[Fetcher] = None  # set when FETCHER=apify; None keeps the link box switched off
    access_code: Optional[str] = None  # set in public mode: visitors must sign in with it
    public = False  # public mode: any Host header is fine, cookies are Secure, per-visitor limits apply
    login_limiter: Optional[RateLimiter] = None
    use_limiter: Optional[RateLimiter] = None
    server_version = "BedahReview"

    # -- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _client(self) -> str:
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()  # the proxy appends the real address last
        return forwarded if self.public and forwarded else self.client_address[0]

    def _authed(self) -> bool:
        if not self.access_code:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and hmac.compare_digest(value, session_token(self.access_code)):
                return True
        return False

    def _login_page(self, status: int, nxt: str, error: str = "") -> None:
        err = '<p class="err" role="alert">%s</p>' % html.escape(error) if error else ""
        page = LOGIN_PAGE % {"error": err, "next": html.escape(safe_next(nxt), quote=True)}
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")  # not 200, so the service worker never caches it as the app

    def _login(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 2000:
            return self._login_page(400, "/", "Bad request.")
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        code, nxt = (form.get("code") or [""])[0], (form.get("next") or ["/"])[0]
        who = self._client()
        if self.login_limiter and self.login_limiter.blocked(who):
            return self._login_page(429, nxt, "Too many tries. Wait 15 minutes. / Terlalu banyak percobaan, tunggu 15 menit.")
        if not self.access_code or not hmac.compare_digest(code.encode("utf-8"), self.access_code.encode("utf-8")):
            if self.login_limiter:
                self.login_limiter.add(who)
            return self._login_page(401, nxt, "Wrong code. / Kode salah.")
        cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (COOKIE, session_token(self.access_code), SESSION_SECS, "; Secure" if self.public else "")
        self.send_response(303)
        self.send_header("Location", safe_next(nxt))
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _host_ok(self) -> bool:
        if self.public:
            return True  # the platform's proxy sets the Host; the access code is the protection here
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def log_message(self, fmt: str, *args: Any) -> None:  # one short line per request, no review text
        # The Share menu sends the review text in the query string (/?text=...), so log the path only.
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?", 1)[0]))

    def _fetch(self, url: str, limit: Any, mode: Any):
        """Fetch reviews from a product link (demo). Returns (reviews, source) or None after sending an error."""
        fetcher = type(self).fetcher
        if fetcher is None:
            self._json(400, {"error": "The link box is switched off. Paste the reviews instead."})
            return None
        try:
            url = check_shopee_product_url(url)
            limit = clamp_limit(limit)
            mode = check_mode(mode)
        except FetchError as exc:
            self._json(400, {"error": str(exc)})
            return None
        try:
            fetched = fetcher.fetch_reviews(url, limit, mode)
        except FetchError as exc:
            self._json(502, {"error": "%s You can paste the reviews instead." % exc, "fallback": "paste"})
            return None
        except Exception as exc:
            sys.stderr.write("fetch failed: %s\n" % type(exc).__name__)
            self._json(502, {"error": "Could not fetch reviews from that link. You can paste the reviews instead.", "fallback": "paste"})
            return None
        source = {"kind": "url", "url": url, "title": product_title_from_url(url), "mode": mode, "count": len(fetched), "cached": bool(getattr(fetcher, "last_from_cache", False))}
        return to_review_dicts(fetched), source

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "Host not allowed"})
        path = self.path.split("?", 1)[0]
        if not self._authed() and path not in PUBLIC_ASSETS:
            if path.startswith("/api/"):
                return self._json(401, {"error": "Please sign in again.", "login": True})
            return self._login_page(401, self.path)
        if path in PAGES:
            return self._send(200, (STATIC / PAGES[path]).read_bytes(), "text/html; charset=utf-8")
        if path in ASSETS:
            name, content_type = ASSETS[path]
            return self._send(200, (STATIC / name).read_bytes(), content_type)
        if path == "/api/config":
            on = type(self).fetcher is not None
            return self._json(200, {"url_box": on, "max_limit": MAX_LIMIT, "default_limit": DEFAULT_LIMIT})
        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "Host not allowed"})
        if self.path == "/login":
            return self._login()
        if self.path != "/api/classify":
            return self._json(404, {"error": "Not found"})
        if not self._authed():
            return self._json(401, {"error": "Please sign in again.", "login": True})
        if self.use_limiter and not self.use_limiter.take(self._client()):
            return self._json(429, {"error": "Too many analyses this hour. Try again later."})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"error": "Send JSON"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "Bad Content-Length"})
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._json(413, {"error": "Paste is empty or too large (limit %d KB)" % (MAX_BODY_BYTES // 1000)})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
            text, url, product = body.get("text", ""), body.get("url", ""), body.get("product", "")
            if not isinstance(text, str) or not isinstance(url, str) or not isinstance(product, str):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "Body must be JSON like {\"text\": \"...\"}"})

        source: Optional[Dict[str, Any]] = None
        if url.strip():
            fetched = self._fetch(url, body.get("limit", DEFAULT_LIMIT), body.get("mode", "complaints"))
            if fetched is None:  # _fetch already sent the error
                return
            reviews, source = fetched
        else:
            reviews = parse_reviews(text)
            name = " ".join(product.split())[:MAX_PRODUCT_CHARS]
            source = {"kind": "paste", "title": name} if name else None
        if not reviews:
            return self._json(400, {"error": "No reviews found. Paste at least one review."})
        if len(reviews) > MAX_REVIEWS:
            return self._json(400, {"error": "%d reviews pasted; v1 handles up to %d at a time." % (len(reviews), MAX_REVIEWS)})

        model = resolve_model()
        try:
            title = (source or {}).get("title") or " ".join(product.split())[:MAX_PRODUCT_CHARS] or None
            results = type(self).classify_fn(reviews, product=title) if title else type(self).classify_fn(reviews)
        except ClassificationError as exc:
            return self._json(502, {"error": "The model's reply could not be used: %s" % str(exc).splitlines()[0]})
        except Exception as exc:  # timeout, rate limit, network: say what happened without a traceback
            sys.stderr.write("classify failed: %s: %s\n" % (type(exc).__name__, exc))
            return self._json(502, {"error": "Could not reach Gemini (%s). Try again in a moment." % type(exc).__name__})
        self._json(200, build_response(reviews, results, model, source))


def make_server(
    port: int = 8000,
    classify_fn: Optional[Callable[..., Any]] = None,
    fetcher: Optional[Fetcher] = None,
    access_code: Optional[str] = None,
    public: bool = False,
    max_per_hour: int = DEFAULT_MAX_PER_HOUR,
) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {})
    if classify_fn is not None:
        handler.classify_fn = staticmethod(classify_fn)
    handler.fetcher = fetcher
    handler.access_code = access_code or None
    handler.public = public
    if access_code:
        handler.login_limiter = RateLimiter(MAX_LOGIN_FAILS, LOGIN_WINDOW_SECS)
    if public:
        handler.use_limiter = RateLimiter(max_per_hour, 3600)
    return ThreadingHTTPServer(("0.0.0.0" if public else "127.0.0.1", port), handler)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the BedahReview paste-in web app locally.")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    load_dotenv()
    public = bool(os.environ.get("PORT") or os.environ.get("BEDAHREVIEW_PUBLIC"))  # Cloud Run sets PORT
    port = args.port or int(os.environ.get("PORT") or 8000)
    access_code = os.environ.get("BEDAHREVIEW_ACCESS_CODE", "")
    if public and len(access_code) < MIN_ACCESS_CODE_CHARS:
        print("Public mode needs BEDAHREVIEW_ACCESS_CODE (at least %d characters), or anyone could spend your Gemini key." % MIN_ACCESS_CODE_CHARS, file=sys.stderr)
        return 1
    try:
        max_per_hour = int(os.environ.get("BEDAHREVIEW_MAX_PER_HOUR") or DEFAULT_MAX_PER_HOUR)
    except ValueError:
        print("BEDAHREVIEW_MAX_PER_HOUR must be a whole number.", file=sys.stderr)
        return 1
    if not has_api_key():
        print("GEMINI_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1
    try:
        fetcher = get_fetcher()
    except FetcherConfigError as exc:
        print("Setup problem: %s" % exc, file=sys.stderr)
        return 1
    try:
        server = make_server(port, fetcher=fetcher, access_code=access_code, public=public, max_per_hour=max_per_hour)
    except OSError as exc:
        print("Could not start on port %d (%s). Try --port 8001." % (port, exc.strerror), file=sys.stderr)
        return 1
    where = "0.0.0.0:%d (public, access code on, %d analyses per visitor per hour)" % (port, max_per_hour) if public else "http://127.0.0.1:%d" % port
    print("BedahReview is running on %s  (model: %s)  Press Ctrl+C to stop." % (where, resolve_model()), flush=True)
    print("Product-link box (demo): %s" % ("ON, paid third-party fetch" if fetcher else "off (set FETCHER=apify in .env to turn on)"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
