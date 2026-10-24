"""Minimal local web app: paste reviews, get the Fix / Fight / Ignore board. Phone-first, installable (PWA).

    python -m bedahreview.web            # then open http://127.0.0.1:8000

Standard library only. It listens on 127.0.0.1 so the Gemini key (read from .env) never leaves
this machine, and it refuses requests whose Host header is not localhost, which blocks other
websites from using your key through the browser.
"""
from __future__ import annotations

import warnings

# Hide the old-Python / LibreSSL notices from Google's libraries (same as `python -W ignore`).
warnings.filterwarnings("ignore")

import argparse
import json
import sys
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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
# Files served from static/ (exact paths only; the query string is ignored, so /?text=... is the page).
PAGES = {"/": "index.html", "/index.html": "index.html"}
ASSETS = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
}


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

    def _host_ok(self) -> bool:
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
        if self.path != "/api/classify":
            return self._json(404, {"error": "Not found"})
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
    port: int = 8000, classify_fn: Optional[Callable[..., Any]] = None, fetcher: Optional[Fetcher] = None
) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {})
    if classify_fn is not None:
        handler.classify_fn = staticmethod(classify_fn)
    handler.fetcher = fetcher
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the BedahReview paste-in web app locally.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    load_dotenv()
    if not has_api_key():
        print("GEMINI_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1
    try:
        fetcher = get_fetcher()
    except FetcherConfigError as exc:
        print("Setup problem: %s" % exc, file=sys.stderr)
        return 1
    try:
        server = make_server(args.port, fetcher=fetcher)
    except OSError as exc:
        print("Could not start on port %d (%s). Try --port 8001." % (args.port, exc.strerror), file=sys.stderr)
        return 1
    print("BedahReview is running on http://127.0.0.1:%d  (model: %s)  Press Ctrl+C to stop." % (args.port, resolve_model()), flush=True)
    print("Product-link box (demo): %s" % ("ON, paid third-party fetch" if fetcher else "off (set FETCHER=apify in .env to turn on)"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
