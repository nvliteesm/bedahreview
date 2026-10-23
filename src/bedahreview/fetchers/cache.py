"""Remember fetched reviews on disk so a demo does not pay for the same link twice."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Tuple

from .base import Fetcher, Review, check_mode, check_shopee_product_url, clamp_limit


class CachedFetcher(Fetcher):
    def __init__(self, inner: Fetcher, cache_dir: Path) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self._local = threading.local()  # the server handles requests on several threads

    @property
    def last_from_cache(self) -> bool:
        return getattr(self._local, "hit", False)

    def _path(self, url: str, mode: str = "latest") -> Path:
        key = url if mode == "latest" else "%s|%s" % (url, mode)  # "latest" keeps the original key, so earlier saved copies still work
        return self.cache_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".json")

    def _read(self, url: str, limit: int, mode: str) -> List[Review]:
        try:
            data = json.loads(self._path(url, mode).read_text(encoding="utf-8"))
            if data["requested"] >= limit:  # a smaller earlier request cannot answer a bigger one
                return [Review(**r) for r in data["reviews"]][:limit]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return []

    def fetch_reviews(self, url: str, limit: int, mode: str = "latest") -> List[Review]:
        url = check_shopee_product_url(url)
        limit = clamp_limit(limit)
        mode = check_mode(mode)
        cached = self._read(url, limit, mode)
        self._local.hit = bool(cached)
        if cached:
            return cached
        reviews = self.inner.fetch_reviews(url, limit, mode)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {"url": url, "mode": mode, "requested": limit, "fetched_at": time.strftime("%Y-%m-%d %H:%M"), "reviews": [asdict(r) for r in reviews]}
            self._path(url, mode).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # a cache that cannot be written is not worth failing the fetch
        return reviews
