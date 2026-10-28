"""Remember fetched reviews on disk for a short while, so a demo does not pay for the same link twice.

A saved copy expires (24 hours by default) and is deleted when it does. Copies from before seller replies were dropped, or
without a save time, are deleted on start-up. Nothing in a copy names a buyer; the review text itself is why it expires.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, List

from .base import Fetcher, Review, check_mode, check_shopee_product_url, clamp_limit

DEFAULT_TTL_HOURS = 24.0
MAX_TTL_HOURS = 168.0  # a week: refuse a typo like 24000
PURGE_EVERY_SECS = 3600


class CachedFetcher(Fetcher):
    def __init__(self, inner: Fetcher, cache_dir: Path, check_url: Callable[[str], str] = check_shopee_product_url,
                 ttl_hours: float = DEFAULT_TTL_HOURS, clock: Callable[[], float] = time.time) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self._check_url = check_url  # the link router passes an already-checked link straight through
        self.ttl_secs = ttl_hours * 3600  # 0 switches saving off altogether
        self._clock = clock
        self._last_purge = 0.0
        self._local = threading.local()  # the server handles requests on several threads
        self.purge()

    @property
    def last_from_cache(self) -> bool:
        return getattr(self._local, "hit", False)

    def _path(self, url: str, mode: str = "latest") -> Path:
        key = url if mode == "latest" else "%s|%s" % (url, mode)  # "latest" keeps the original key, so earlier saved copies still work
        return self.cache_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".json")

    def _stale(self, data: Any) -> bool:
        """Too old, saved in the future, saved without a time, or from the days when seller replies were kept."""
        saved = data.get("saved") if isinstance(data, dict) else None
        if not isinstance(saved, (int, float)) or isinstance(saved, bool):
            return True
        age = self._clock() - saved
        if age > self.ttl_secs or age < -60:
            return True
        reviews = data.get("reviews")
        return not isinstance(reviews, list) or any(not isinstance(r, dict) or "seller_reply" in r for r in reviews)

    def purge(self) -> None:
        """Delete every saved copy that is stale or unreadable."""
        self._last_purge = self._clock()
        try:
            files = list(self.cache_dir.glob("*.json"))
        except OSError:
            return
        for path in files:
            try:
                stale = self.ttl_secs <= 0 or self._stale(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                stale = True
            if stale:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _read(self, url: str, limit: int, mode: str) -> List[Review]:
        if self.ttl_secs <= 0:
            return []
        path = self._path(url, mode)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if self._stale(data):
                path.unlink()
                return []
            if data["requested"] >= limit:  # a smaller earlier request cannot answer a bigger one
                return [Review(**r) for r in data["reviews"]][:limit]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return []

    def fetch_reviews(self, url: str, limit: int, mode: str = "latest") -> List[Review]:
        url = self._check_url(url)
        limit = clamp_limit(limit)
        mode = check_mode(mode)
        cached = self._read(url, limit, mode)
        self._local.hit = bool(cached)
        if cached:
            return cached
        reviews = self.inner.fetch_reviews(url, limit, mode)
        if self.ttl_secs <= 0:
            return reviews
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            now = self._clock()
            payload = {"url": url, "mode": mode, "requested": limit, "saved": int(now), "fetched_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), "reviews": [asdict(r) for r in reviews]}
            self._path(url, mode).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if now - self._last_purge > PURGE_EVERY_SECS:
                self.purge()
        except OSError:
            pass  # a cache that cannot be written is not worth failing the fetch
        return reviews
