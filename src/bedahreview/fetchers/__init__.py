"""Optional, demo-only fetching of reviews from a product link (Tokopedia, Lazada, TikTok Shop). Paste/upload stays the default."""
from __future__ import annotations

import os
from typing import Optional

from ..classifier import ROOT
from .base import MAX_LIMIT, Fetcher, FetcherConfigError, FetchError, LinkError, Review, product_title_from_url, to_review_dicts
from .budget import DAILY_BUDGET_CEILING_USD, DEFAULT_DAILY_BUDGET_USD, BudgetError, DailyBudget
from .cache import DEFAULT_TTL_HOURS, MAX_TTL_HOURS, CachedFetcher
from .platforms import DEFAULT_MAX_COST_USD, LinkRouter

CACHE_DIR = ROOT / "fetch_cache"
MAX_COST_CEILING_USD = 1.0  # refuse an APIFY_MAX_COST_USD typo like 50

__all__ = ["get_fetcher", "Fetcher", "FetchError", "LinkError", "FetcherConfigError", "BudgetError", "Review", "MAX_LIMIT", "product_title_from_url", "to_review_dicts"]


def get_fetcher() -> Optional[Fetcher]:
    """The fetcher chosen by FETCHER in .env: None for "paste" (the default), a link router (Tokopedia, Lazada, TikTok Shop; cached, spend-capped) for "apify"."""
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
    try:
        daily = float(os.environ.get("APIFY_DAILY_BUDGET_USD") or DEFAULT_DAILY_BUDGET_USD)
    except ValueError:
        raise FetcherConfigError("APIFY_DAILY_BUDGET_USD must be a number like 1.00.")
    if not 0 < daily <= DAILY_BUDGET_CEILING_USD:
        raise FetcherConfigError("APIFY_DAILY_BUDGET_USD must be above 0 and at most %.2f." % DAILY_BUDGET_CEILING_USD)
    try:
        hours = float(os.environ.get("FETCH_CACHE_HOURS") or DEFAULT_TTL_HOURS)
    except ValueError:
        raise FetcherConfigError("FETCH_CACHE_HOURS must be a number like 24.")
    if not 0 <= hours <= MAX_TTL_HOURS:
        raise FetcherConfigError("FETCH_CACHE_HOURS must be between 0 (do not save) and %d." % MAX_TTL_HOURS)
    return LinkRouter(token, cap, CACHE_DIR, budget=DailyBudget(daily), cache_ttl_hours=hours)
