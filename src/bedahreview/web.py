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
import re
import secrets
import socket
import sys
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import parse_qs

from .auth import SupabaseAuth
from .classifier import ClassificationError, classify, resolve_model
from .competitors import MAX_COMPETITORS, compare_competitors, suggest_competitors
from .env import has_api_key, load_dotenv
from .events import clean_event, log_line
from .fetchers import MAX_LIMIT, BudgetError, Fetcher, FetcherConfigError, FetchError, get_fetcher, product_title_from_url, to_review_dicts
from .fetchers.base import check_mode, check_shopee_product_url, clamp_limit
from .fetchers.search import SEARCH_PLATFORMS
from .parse import parse_reviews

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 200_000
MAX_REVIEWS = 50  # v1 cap: keeps one paste to a handful of API calls
BUCKET_ORDER = ["fix", "fight", "check", "ignore", "keep"]
DEFAULT_LIMIT = 10  # reviews fetched from a link when the seller does not choose
MAX_PRODUCT_CHARS = 150
COOKIE = "br_session"
SESSION_SECS = 14 * 24 * 3600  # a sign-in lasts two weeks, then the tester enters the code again
CSRF_COOKIE = "br_csrf"
CSRF_SECS = 3600
SHARED_NAME = "shared"  # the name of the single BEDAHREVIEW_ACCESS_CODE
NAME_RE = re.compile(r"^[a-z0-9_-]{1,20}$")
MIN_ACCESS_CODE_CHARS = 8
MAX_LOGIN_FAILS = 5  # per visitor, per LOGIN_WINDOW_SECS
LOGIN_WINDOW_SECS = 15 * 60
DEFAULT_MAX_PER_HOUR = 30  # analyses per visitor per hour in public mode
MAX_EVENT_BYTES = 2000
READ_TIMEOUT_SECS = 20  # a visitor who stops sending (or sends one byte at a time) is cut off after this long
MAX_CONNECTIONS = 64  # open connections at once; more get an immediate 503
DEFAULT_MAX_CONCURRENT = 6  # analyses (model or paid-fetch work) running at once; more get a 503 and a Retry-After
HEAVY_PATHS = ("/api/classify", "/api/competitors/suggest", "/api/competitors/compare")
MAX_DRAIN_BYTES = 2_000_000  # an oversized upload is read and thrown away up to this much, so the error reply is not lost to a connection reset
DRAIN_SECS = 5
_BUSY_BODY = b'{"error": "Server is busy."}'
BUSY_RESPONSE = (b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                 + str(len(_BUSY_BODY)).encode() + b"\r\n\r\n" + _BUSY_BODY)  # sent as is when the connection cap is hit
EVENTS_PER_HOUR = 600  # per visitor in public mode: a stuck page cannot flood the log
DEFAULT_PAID_PER_HOUR = 6 # paid actions (link fetch, find or compare competitors) per visitor per hour in public mode: lower than plain pasting
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

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.time) -> None:
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: Dict[str, Deque[float]] = {}
        self._last_prune = clock()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Forget visitors with no recent events, so the table cannot grow for ever. Runs at most once per window."""
        if now - self._last_prune < self.window:
            return
        self._last_prune = now
        for key in [k for k, hits in self._hits.items() if not hits or now - hits[-1] > self.window]:
            del self._hits[key]

    def _recent(self, key: str, now: float) -> Deque[float]:
        self._prune(now)
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def size(self) -> int:
        with self._lock:
            return len(self._hits)

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, self._clock())) >= self.limit

    def add(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._recent(key, now).append(now)

    def take(self, key: str) -> bool:
        """Count one event; False when the key is already at its limit."""
        with self._lock:
            now = self._clock()
            hits = self._recent(key, now)
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def parse_access_codes(text: str) -> Dict[str, str]:
    """'ani:code1234,budi:code5678' -> {'ani': 'code1234', 'budi': 'code5678'}. One entry per tester, so one tester can be cut off by removing theirs."""
    codes: Dict[str, str] = {}
    for part in (p.strip() for p in (text or "").split(",")):
        if not part:
            continue
        name, sep, code = part.partition(":")
        name = name.strip().lower()
        if not sep or not NAME_RE.match(name):
            raise ValueError("Each BEDAHREVIEW_ACCESS_CODES entry must look like name:code, with a name of 1 to 20 lowercase letters, digits, - or _.")
        if len(code) < MIN_ACCESS_CODE_CHARS:
            raise ValueError("The code for %r must be at least %d characters." % (name, MIN_ACCESS_CODE_CHARS))
        if name in codes or code in codes.values():
            raise ValueError("Each tester needs a different name and a different code (%r repeats)." % name)
        codes[name] = code
    return codes


def session_token(name: str, code: str, expires: int) -> str:
    """name.expiry.signature. The signature is made with that tester's code, so removing or changing the code ends every sign-in of theirs."""
    sig = hmac.new(code.encode("utf-8"), ("session|%s|%d" % (name, expires)).encode("utf-8"), hashlib.sha256).hexdigest()
    return "%s.%d.%s" % (name, expires, sig)


def check_session(token: str, codes: Dict[str, str], now: float) -> Optional[str]:
    """The tester's name when the token is genuine, unexpired and their code still exists; otherwise None."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    name, expires = parts[0], int(parts[1])
    code = codes.get(name)
    if not code or expires < now or expires - now > SESSION_SECS + 60:
        return None
    return name if hmac.compare_digest(token.encode("utf-8"), session_token(name, code, expires).encode("utf-8")) else None


def _csrf_key(codes: Dict[str, str]) -> bytes:
    return hashlib.sha256(("csrf|" + "|".join(sorted(codes.values()))).encode("utf-8")).digest()


def make_csrf(codes: Dict[str, str]) -> str:
    nonce = secrets.token_hex(16)
    return nonce + "." + hmac.new(_csrf_key(codes), nonce.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def csrf_ok(codes: Dict[str, str], cookie_value: str, form_value: str) -> bool:
    """Signed double-submit: the form field must equal the cookie, and both must be one we issued."""
    nonce, _, sig = cookie_value.partition(".")
    good = hmac.new(_csrf_key(codes), nonce.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return bool(nonce) and hmac.compare_digest(sig.encode("utf-8"), good.encode("utf-8")) and hmac.compare_digest(cookie_value.encode("utf-8"), form_value.encode("utf-8"))


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
<input type="hidden" name="csrf" value="%(csrf)s">
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


class _CountingReader:
    """Wraps the request stream and counts what was read, so any unread part of an upload can be thrown away afterwards."""

    def __init__(self, raw: Any) -> None:
        self._raw, self.count = raw, 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.count += len(data)
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class BoundedServer(ThreadingHTTPServer):
    """A threading server that refuses new connections beyond a cap instead of starting threads without limit."""

    daemon_threads = True

    def __init__(self, address: Any, handler: Any, max_connections: int = MAX_CONNECTIONS) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(BUSY_RESPONSE)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    classify_fn: Callable[..., List[Dict[str, Any]]] = staticmethod(classify)  # replaced in tests
    suggest_fn: Callable[..., Dict[str, Any]] = staticmethod(suggest_competitors)  # replaced in tests
    compare_fn: Callable[..., Dict[str, Any]] = staticmethod(compare_competitors)  # replaced in tests
    fetcher: Optional[Fetcher] = None  # set when FETCHER=apify; None keeps the link box switched off
    access_codes: Dict[str, str] = {}  # tester name -> code; empty means no sign-in (local use)
    public = False  # public mode: any Host header is fine, cookies are Secure, per-visitor limits apply
    supabase_auth: Optional[SupabaseAuth] = None  # checks Supabase sign-in tokens on this server; None means only the access-code cookie works
    login_limiter: Optional[RateLimiter] = None
    use_limiter: Optional[RateLimiter] = None
    paid_limiter: Optional[RateLimiter] = None
    event_limiter: Optional[RateLimiter] = None
    analysis_slots: Any = threading.BoundedSemaphore(DEFAULT_MAX_CONCURRENT)  # replaced per server in make_server
    timeout = READ_TIMEOUT_SECS  # socketserver applies this to the connection: slow or silent visitors are dropped

    def _event(self) -> None:
        """One anonymous usage event (fixed list, fixed fields: see events.py). Behind the access code; written to stdout as one JSON line."""
        if not self._authed():
            return self._json(401, {"error": "Please sign in again.", "login": True})
        if self.event_limiter and not self.event_limiter.take(self._client()):
            return self._json(429, {"error": "Too many events."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if "application/json" not in (self.headers.get("Content-Type") or "") or not 0 < length <= MAX_EVENT_BYTES:
                raise ValueError
            event = clean_event(json.loads(self.rfile.read(length).decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "Not an allowed event."})
        sys.stdout.write(log_line(event) + "\n")
        sys.stdout.flush()
        self._json(200, {"ok": True})

    def _paid_ok(self) -> bool:
        """Count one paid action for this visitor; send the 429 and return False when they are at the hourly limit."""
        if self.paid_limiter and not self.paid_limiter.take(self._client()):
            self._json(429, {"error": "Too many link look-ups this hour. Try again later.", "limit": "hour"})
            return False
        return True
    server_version = "BedahReview"

    # -- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[List[Any]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in extra or []:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":  # a HEAD reply has the same headers, including Content-Length, and no body
            self.wfile.write(body)

    def _json(self, status: int, payload: Dict[str, Any], extra: Optional[List[Any]] = None) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _client(self) -> str:
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()  # the proxy appends the real address last
        return forwarded if self.public and forwarded else self.client_address[0]

    def _cookie(self, wanted: str) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == wanted:
                return value
        return ""

    def _cookie_flags(self) -> str:
        return "Path=/; HttpOnly; SameSite=Lax%s" % ("; Secure" if self.public else "")

    def _bearer_user(self) -> Optional[str]:
        """The Supabase user id from an "Authorization: Bearer <token>" header, or None. A token is not a cookie, so a form on another site cannot send it."""
        auth = type(self).supabase_auth
        header = self.headers.get("Authorization") or ""
        if auth is None or not header[:7].lower() == "bearer ":
            return None
        return auth.verify(header[7:].strip())

    def _authed(self) -> bool:
        if not self.access_codes:
            return True  # local use: no sign-in at all, as before (the Supabase sign-in screens arrive in the next step)
        if self._bearer_user():
            return True
        return check_session(self._cookie(COOKIE), self.access_codes, time.time()) is not None

    def _login_page(self, status: int, nxt: str, error: str = "") -> None:
        err = '<p class="err" role="alert">%s</p>' % html.escape(error) if error else ""
        token = make_csrf(self.access_codes)
        page = LOGIN_PAGE % {"error": err, "next": html.escape(safe_next(nxt), quote=True), "csrf": token}
        # not 200, so the service worker never caches it as the app
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8", [("Set-Cookie", "%s=%s; Max-Age=%d; %s" % (CSRF_COOKIE, token, CSRF_SECS, self._cookie_flags()))])

    def _login(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 2000:
            return self._login_page(400, "/", "Bad request.")
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        code, nxt, csrf = (form.get("code") or [""])[0], (form.get("next") or ["/"])[0], (form.get("csrf") or [""])[0]
        who = self._client()
        if not self.access_codes:
            return self._login_page(400, nxt, "Bad request.")
        if not csrf_ok(self.access_codes, self._cookie(CSRF_COOKIE), csrf):
            return self._login_page(400, nxt, "This page expired. Try again. / Halaman kedaluwarsa, coba lagi.")
        if self.login_limiter and self.login_limiter.blocked(who):
            return self._login_page(429, nxt, "Too many tries. Wait 15 minutes. / Terlalu banyak percobaan, tunggu 15 menit.")
        found = None
        for name, known in self.access_codes.items():  # every code is compared, so the time taken does not say which one is close
            if hmac.compare_digest(code.encode("utf-8"), known.encode("utf-8")):
                found = name
        if found is None:
            if self.login_limiter:
                self.login_limiter.add(who)
            return self._login_page(401, nxt, "Wrong code. / Kode salah.")
        token = session_token(found, self.access_codes[found], int(time.time()) + SESSION_SECS)
        self.send_response(303)
        self.send_header("Location", safe_next(nxt))
        self.send_header("Set-Cookie", "%s=%s; Max-Age=%d; %s" % (COOKIE, token, SESSION_SECS, self._cookie_flags()))
        self.send_header("Set-Cookie", "%s=; Max-Age=0; %s" % (CSRF_COOKIE, self._cookie_flags()))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _logout(self) -> None:
        """End this browser's sign-in. JSON only, so another website cannot trigger it with a plain form post."""
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"error": "Send JSON"})
        body = json.dumps({"ok": True}).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8", [("Set-Cookie", "%s=; Max-Age=0; %s" % (COOKIE, self._cookie_flags()))])

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
            self._json(400, {"error": "The link box is switched off. Paste the reviews instead.", "code": "box_off"})
            return None
        try:
            check_link = getattr(fetcher, "check_link", None)  # the link router knows every supported platform; older fetchers are Shopee-only
            url = check_link(url) if check_link else check_shopee_product_url(url)
            limit = clamp_limit(limit)
            mode = check_mode(mode)
        except FetchError as exc:  # includes LinkError
            self._json(400, {"error": str(exc), "code": exc.code})
            return None
        if not self._paid_ok():  # a saved copy costs nothing but is counted too; the limit is per visitor and generous enough
            return None
        try:
            fetched = fetcher.fetch_reviews(url, limit, mode)
        except BudgetError:
            self._json(429, {"error": "Demo limit reached today. Paste the reviews instead.", "fallback": "paste", "limit": "budget"})
            return None
        except FetchError as exc:
            self._json(502, {"error": "%s You can paste the reviews instead." % exc, "fallback": "paste", "code": exc.code})
            return None
        except Exception as exc:
            sys.stderr.write("fetch failed: %s\n" % type(exc).__name__)
            self._json(502, {"error": "Could not fetch reviews from that link. You can paste the reviews instead.", "fallback": "paste", "code": "fetch_failed"})
            return None
        listing = next((r.product for r in fetched if getattr(r, "product", None)), None)
        title = listing or ("" if check_link else product_title_from_url(url))  # the router's links carry no title; its reviews do
        source = {"kind": "url", "url": url, "title": title[:MAX_PRODUCT_CHARS], "mode": mode, "count": len(fetched), "cached": bool(getattr(fetcher, "last_from_cache", False))}
        return to_review_dicts(fetched), source

    def _suggest(self) -> None:
        """Competitor analysis, step 1: from the seller's own link, suggest comparable products to confirm. Paid search, so it counts as an analysis."""
        if not self._authed():
            return self._json(401, {"error": "Please sign in again.", "login": True})
        if self.use_limiter and not self.use_limiter.take(self._client()):
            return self._json(429, {"error": "Too many analyses this hour. Try again later.", "code": "too_many"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if "application/json" not in (self.headers.get("Content-Type") or "") or not 0 < length <= MAX_BODY_BYTES:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            url, title = body.get("url", ""), body.get("title", "")
            if not isinstance(url, str) or not isinstance(title, str):
                raise ValueError
        except (ValueError, UnicodeDecodeError, AttributeError):
            return self._json(400, {"error": "Body must be JSON like {\"url\": \"...\", \"title\": \"...\"}"})
        fetcher = type(self).fetcher
        search = getattr(fetcher, "search", None)
        if search is None:
            return self._json(400, {"error": "Finding competitors needs the link box. Paste competitor links instead.", "fallback": "paste", "code": "box_off"})
        title = " ".join(title.split())[:MAX_PRODUCT_CHARS]
        if not title:
            return self._json(400, {"error": "The product title is missing, so competitors cannot be searched.", "code": "no_title"})
        try:
            url = fetcher.check_link(url)
        except FetchError as exc:
            return self._json(400, {"error": str(exc), "code": exc.code})
        platform = fetcher.last_platform
        if platform not in SEARCH_PLATFORMS:
            return self._json(400, {"error": "Finding competitors is not available for this platform yet. Paste competitor links instead.", "fallback": "paste", "code": "search_platform"})
        if not self._paid_ok():
            return
        try:
            result = type(self).suggest_fn(search, platform, url, title)
        except BudgetError:
            return self._json(429, {"error": "Demo limit reached today. Try again tomorrow.", "limit": "budget"})
        except FetchError as exc:
            return self._json(502, {"error": "%s You can paste competitor links instead." % exc, "fallback": "paste", "code": exc.code})
        except Exception as exc:  # model or network trouble: say what happened without a traceback
            sys.stderr.write("suggest failed: %s\n" % type(exc).__name__)
            return self._json(502, {"error": "Could not look for competitors (%s). You can paste competitor links instead." % type(exc).__name__, "fallback": "paste", "code": "suggest_failed"})
        self._json(200, result)

    def _compare(self) -> None:
        """Competitor analysis, step 2: fetch a small mix of reviews from the confirmed competitors and compare themes. Paid, so it counts as an analysis; nothing is cached or kept."""
        if not self._authed():
            return self._json(401, {"error": "Please sign in again.", "login": True})
        if self.use_limiter and not self.use_limiter.take(self._client()):
            return self._json(429, {"error": "Too many analyses this hour. Try again later.", "code": "too_many"})
        fetcher = type(self).fetcher
        fetch, check_link = getattr(fetcher, "fetch_fresh", None), getattr(fetcher, "check_link", None)
        if fetch is None or check_link is None:
            return self._json(400, {"error": "Comparing needs the link box. Paste competitor links instead.", "fallback": "paste", "code": "box_off"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if "application/json" not in (self.headers.get("Content-Type") or "") or not 0 < length <= MAX_BODY_BYTES:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            own = [{"theme": i["theme"], "bucket": i["bucket"], "fake": bool(i["fake"])} for i in body.get("own", [])]
            if len(own) > MAX_REVIEWS or not all(isinstance(i["theme"], str) and isinstance(i["bucket"], str) for i in own):
                raise ValueError
            title = " ".join(str(body.get("title") or "").split())[:MAX_PRODUCT_CHARS]
            wanted = [{"url": str(c["url"]), "title": " ".join(str(c.get("title") or "").split())[:MAX_PRODUCT_CHARS]} for c in body["competitors"]]
            if not wanted or len(wanted) > MAX_COMPETITORS:
                raise ValueError
            own_url = body.get("url") or ""
        except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
            return self._json(400, {"error": "Choose 1 to %d competitors to compare." % MAX_COMPETITORS, "code": "pick_count"})
        try:
            mine = check_link(own_url) if own_url else ""
            competitors = []
            for entry in wanted:
                clean = check_link(entry["url"])
                if clean != mine and all(clean != c["url"] for c in competitors):
                    competitors.append({"url": clean, "title": entry["title"]})
        except FetchError as exc:
            return self._json(400, {"error": str(exc), "code": exc.code})
        if not competitors:
            return self._json(400, {"error": "Choose competitors other than your own product.", "code": "pick_other"})
        if not self._paid_ok():
            return
        try:
            result = type(self).compare_fn(fetch, type(self).classify_fn, own, title, competitors)
        except BudgetError:
            return self._json(429, {"error": "Demo limit reached today. Try again tomorrow.", "limit": "budget"})
        except FetchError as exc:
            return self._json(502, {"error": "%s You can try other competitors." % exc, "fallback": "other", "code": exc.code})
        except Exception as exc:  # model or network trouble: say what happened without a traceback
            sys.stderr.write("compare failed: %s\n" % type(exc).__name__)
            return self._json(502, {"error": "Could not compare (%s). Try again in a moment." % type(exc).__name__, "code": "compare_failed"})
        self._json(200, result)

    # -- routes ----------------------------------------------------------
    def _guarded(self, work: Callable[[], None]) -> None:
        """Run one request. A visitor who has gone away is ignored; any other failure becomes a plain 500 that names only the error type, never request content."""
        try:
            work()
        except (ConnectionError, socket.timeout):
            self.close_connection = True
        except Exception as exc:
            sys.stderr.write("request failed: %s\n" % type(exc).__name__)
            try:
                self._json(500, {"error": "Something went wrong on the server. Try again.", "code": "server"})
            except Exception:
                self.close_connection = True

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/healthz":  # for the platform's health checks: public, cheap, says nothing about the app
            return self._send(200, b'{"ok": true}', "application/json; charset=utf-8")
        self._guarded(self._get)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        reader = _CountingReader(self.rfile)
        self.rfile = reader
        try:
            if self.path in HEAVY_PATHS:
                if not self.analysis_slots.acquire(blocking=False):
                    return self._json(503, {"error": "The server is busy. Try again in a moment.", "limit": "busy"}, [("Retry-After", "5")])
                try:
                    self._guarded(self._post)
                finally:
                    self.analysis_slots.release()
            else:
                self._guarded(self._post)
        finally:
            self._drain_unread(reader)

    def _drain_unread(self, reader: _CountingReader) -> None:
        """Read and discard the rest of an upload we answered early (too big, wrong type), so closing does not reset the connection and lose our reply."""
        try:
            left = min(int(self.headers.get("Content-Length") or 0) - reader.count, MAX_DRAIN_BYTES)
            deadline = time.time() + DRAIN_SECS
            self.connection.settimeout(2)
            while left > 0 and time.time() < deadline:
                data = reader.read(min(left, 65536))
                if not data:
                    break
                left -= len(data)
        except (OSError, ValueError):
            pass

    def _method_not_allowed(self) -> None:
        self._json(405, {"error": "Method not allowed"}, [("Allow", "GET, HEAD, POST")])

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def _get(self) -> None:
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
            return self._json(200, {"url_box": on, "max_limit": MAX_LIMIT, "default_limit": DEFAULT_LIMIT, "access": bool(self.access_codes)})
        self._json(404, {"error": "Not found"})

    def _post(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "Host not allowed"})
        if self.path == "/login":
            return self._login()
        if self.path == "/logout":
            return self._logout()
        if self.path == "/api/event":
            return self._event()
        if self.path == "/api/competitors/suggest":
            return self._suggest()
        if self.path == "/api/competitors/compare":
            return self._compare()
        if self.path != "/api/classify":
            return self._json(404, {"error": "Not found"})
        if not self._authed():
            return self._json(401, {"error": "Please sign in again.", "login": True})
        if self.use_limiter and not self.use_limiter.take(self._client()):
            return self._json(429, {"error": "Too many analyses this hour. Try again later.", "code": "too_many"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"error": "Send JSON"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "Bad Content-Length"})
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._json(413, {"error": "Paste is empty or too large (limit %d KB)" % (MAX_BODY_BYTES // 1000), "code": "too_large"})
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
            return self._json(400, {"error": "No reviews found. Paste at least one review.", "code": "no_parse"})
        if len(reviews) > MAX_REVIEWS:
            return self._json(400, {"error": "%d reviews pasted; v1 handles up to %d at a time." % (len(reviews), MAX_REVIEWS), "code": "too_many_reviews", "max": MAX_REVIEWS})

        model = resolve_model()
        try:
            title = (source or {}).get("title") or " ".join(product.split())[:MAX_PRODUCT_CHARS] or None
            results = type(self).classify_fn(reviews, product=title) if title else type(self).classify_fn(reviews)
        except ClassificationError as exc:
            return self._json(502, {"error": "The model's reply could not be used: %s" % str(exc).splitlines()[0], "code": "model_bad"})
        except Exception as exc:  # timeout, rate limit, network: say what happened without a traceback
            code = getattr(exc, "code", None)  # an HTTP status such as 429 or 503 helps; the message could echo what was sent, so it is not logged
            sys.stderr.write("classify failed: %s%s\n" % (type(exc).__name__, " (code %s)" % code if isinstance(code, int) else ""))
            return self._json(502, {"error": "Could not reach Gemini (%s). Try again in a moment." % type(exc).__name__, "code": "model_down"})
        self._json(200, build_response(reviews, results, model, source))


def make_server(
    port: int = 8000,
    classify_fn: Optional[Callable[..., Any]] = None,
    suggest_fn: Optional[Callable[..., Any]] = None,
    compare_fn: Optional[Callable[..., Any]] = None,
    fetcher: Optional[Fetcher] = None,
    access_code: Optional[str] = None,
    public: bool = False,
    access_codes: Optional[Dict[str, str]] = None,
    supabase_auth: Optional[SupabaseAuth] = None,
    max_per_hour: int = DEFAULT_MAX_PER_HOUR,
    paid_per_hour: int = DEFAULT_PAID_PER_HOUR,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    max_connections: int = MAX_CONNECTIONS,
    read_timeout: float = READ_TIMEOUT_SECS,
) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {})
    if classify_fn is not None:
        handler.classify_fn = staticmethod(classify_fn)
    if suggest_fn is not None:
        handler.suggest_fn = staticmethod(suggest_fn)
    if compare_fn is not None:
        handler.compare_fn = staticmethod(compare_fn)
    handler.fetcher = fetcher
    handler.analysis_slots = threading.BoundedSemaphore(max_concurrent)
    handler.timeout = read_timeout
    codes = dict(access_codes or {})
    if access_code:
        codes.setdefault(SHARED_NAME, access_code)
    handler.access_codes = codes
    handler.public = public
    handler.supabase_auth = supabase_auth
    if codes:
        handler.login_limiter = RateLimiter(MAX_LOGIN_FAILS, LOGIN_WINDOW_SECS)
    if public:
        handler.use_limiter = RateLimiter(max_per_hour, 3600)
        handler.paid_limiter = RateLimiter(paid_per_hour, 3600)
        handler.event_limiter = RateLimiter(EVENTS_PER_HOUR, 3600)
    return BoundedServer(("0.0.0.0" if public else "127.0.0.1", port), handler, max_connections)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the BedahReview paste-in web app locally.")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    load_dotenv()
    public = bool(os.environ.get("PORT") or os.environ.get("BEDAHREVIEW_PUBLIC"))  # Cloud Run sets PORT
    port = args.port or int(os.environ.get("PORT") or 8000)
    access_code = os.environ.get("BEDAHREVIEW_ACCESS_CODE", "")
    try:
        access_codes = parse_access_codes(os.environ.get("BEDAHREVIEW_ACCESS_CODES", ""))
    except ValueError as exc:
        print("Setup problem: %s" % exc, file=sys.stderr)
        return 1
    if access_code and len(access_code) < MIN_ACCESS_CODE_CHARS:
        print("BEDAHREVIEW_ACCESS_CODE must be at least %d characters." % MIN_ACCESS_CODE_CHARS, file=sys.stderr)
        return 1
    if public and not access_code and not access_codes:
        print("Public mode needs BEDAHREVIEW_ACCESS_CODE or BEDAHREVIEW_ACCESS_CODES (codes of at least %d characters), or anyone could spend your Gemini key." % MIN_ACCESS_CODE_CHARS, file=sys.stderr)
        return 1
    try:
        max_per_hour = int(os.environ.get("BEDAHREVIEW_MAX_PER_HOUR") or DEFAULT_MAX_PER_HOUR)
    except ValueError:
        print("BEDAHREVIEW_MAX_PER_HOUR must be a whole number.", file=sys.stderr)
        return 1
    try:
        paid_per_hour = int(os.environ.get("BEDAHREVIEW_PAID_PER_HOUR") or DEFAULT_PAID_PER_HOUR)
    except ValueError:
        print("BEDAHREVIEW_PAID_PER_HOUR must be a whole number.", file=sys.stderr)
        return 1
    try:
        max_concurrent = int(os.environ.get("BEDAHREVIEW_MAX_CONCURRENT") or DEFAULT_MAX_CONCURRENT)
        if max_concurrent < 1:
            raise ValueError
    except ValueError:
        print("BEDAHREVIEW_MAX_CONCURRENT must be a whole number of 1 or more.", file=sys.stderr)
        return 1
    supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
    if supabase_url and not re.match(r"^https://[a-z0-9-]+\.supabase\.co/?$", supabase_url):
        print("SUPABASE_URL must look like https://<project>.supabase.co", file=sys.stderr)
        return 1
    supabase_auth = SupabaseAuth(supabase_url) if supabase_url else None
    if not has_api_key():
        print("GEMINI_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1
    try:
        fetcher = get_fetcher()
    except FetcherConfigError as exc:
        print("Setup problem: %s" % exc, file=sys.stderr)
        return 1
    try:
        server = make_server(port, fetcher=fetcher, access_code=access_code, access_codes=access_codes, supabase_auth=supabase_auth, public=public, max_per_hour=max_per_hour, paid_per_hour=paid_per_hour, max_concurrent=max_concurrent)
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
