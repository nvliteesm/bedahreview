"""Optional, demo-only fetching of reviews from a product link. Paste/upload stays the default."""
from __future__ import annotations

import os
from typing import Optional

from ..classifier import ROOT
from .apify_shopee import DEFAULT_MAX_COST_USD, ApifyShopeeFetcher
from .base import MAX_LIMIT, Fetcher, FetcherConfigError, FetchError, Review, to_review_dicts
from .cache import CachedFetcher

CACHE_DIR = ROOT / "fetch_cache"
MAX_COST_CEILING_USD = 1.0  # refuse an APIFY_MAX_COST_USD typo like 50

__all__ = ["get_fetcher", "Fetcher", "FetchError", "FetcherConfigError", "Review", "MAX_LIMIT", "to_review_dicts"]


def get_fetcher() -> Optional[Fetcher]:
    """The fetcher chosen by FETCHER in .env: None for "paste" (the default), a cached Apify fetcher for "apify"."""
    choice = os.environ.get("FETCHER", "paste").strip().lower() or "paste"
    if choice == "paste":
        return None
    if choice != "apify":
        raise FetcherConfigError("FETCHER must be 'paste' or 'apify', not %r." % choice)
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise FetcherConfigError("FETCHER=apify needs APIFY_TOKEN in .env (see .env.example).")
    try:
        cap = float(os.environ.get("APIFY_MAX_COST_USD") or DEFAULT_MAX_COST_USD)
    except ValueError:
        raise FetcherConfigError("APIFY_MAX_COST_USD must be a number like 0.05.")
    if not 0 < cap <= MAX_COST_CEILING_USD:
        raise FetcherConfigError("APIFY_MAX_COST_USD must be above 0 and at most %.2f." % MAX_COST_CEILING_USD)
    return CachedFetcher(ApifyShopeeFetcher(token, max_cost_usd=cap), CACHE_DIR)
