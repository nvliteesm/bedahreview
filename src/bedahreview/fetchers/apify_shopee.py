"""Fetch Shopee reviews through the Apify actor zen-studio/shopee-product-reviews-scraper (demo only).

Platform terms for Shopee are not verified. This never tries to get around blocks, logins or captchas:
if the service returns nothing, the caller shows the seller a message and they paste instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .base import Fetcher, FetchError, Review, check_shopee_product_url, clamp_limit

ACTOR_ID = "zen-studio/shopee-product-reviews-scraper"
# Prices seen on the actor page on 2026-10-23 (free Apify plan). Used only for the estimate below.
PRICE_PER_REVIEW_USD = 0.00399
START_FEE_USD = 0.008
DEFAULT_MAX_COST_USD = 0.05
RUN_TIMEOUT_SECS = 120


def estimated_cost_usd(limit: int) -> float:
    return START_FEE_USD + limit * PRICE_PER_REVIEW_USD


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _variant(item: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []
    for variation in item.get("variations") or []:
        if not isinstance(variation, dict):
            continue
        options = variation.get("options")
        chosen = [o for o in options if isinstance(o, str)] if isinstance(options, list) else []
        for label in chosen or [variation.get("name")]:
            if isinstance(label, str) and label.strip() and label.strip() not in parts:
                parts.append(label.strip())
    return ", ".join(parts) or None


def _date(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000 if value > 1e11 else value  # Shopee uses seconds or milliseconds
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value[:10] if len(value) >= 10 and value[4:5] == "-" else value
    return ""


def _reply(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("comment", "text", "content", "reply"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return None


def review_from_item(item: Dict[str, Any]) -> Review:
    """Copy only the fields we need. The item's author name, id and avatar are never read."""
    star = item.get("ratingStar")
    return Review(
        star=star if isinstance(star, int) and 1 <= star <= 5 else None,
        text=_text(item.get("comment")),
        variant=_variant(item),
        date=_date(item.get("createdAt")),
        photo_count=len(item["images"]) if isinstance(item.get("images"), list) else 0,
        seller_reply=_reply(item.get("shopReply")),
    )


class ApifyShopeeFetcher(Fetcher):
    def __init__(self, token: str, max_cost_usd: float = DEFAULT_MAX_COST_USD, client: Optional[Any] = None) -> None:
        self.max_cost_usd = max_cost_usd
        if client is None:
            from apify_client import ApifyClient

            client = ApifyClient(token)
        self._client = client

    def fetch_reviews(self, url: str, limit: int) -> List[Review]:
        url = check_shopee_product_url(url)
        limit = clamp_limit(limit)
        run_input = {
            "startUrls": [{"url": url}],
            "maxReviewsPerProduct": limit,
            "contentFilter": "with comments",  # the newest reviews are often blank 5-stars
        }
        try:
            run = self._client.actor(ACTOR_ID).call(
                run_input=run_input,
                max_total_charge_usd=Decimal(str(self.max_cost_usd)),  # hard spending cap for this run
                timeout_secs=RUN_TIMEOUT_SECS,
                wait_secs=RUN_TIMEOUT_SECS,
            )
            if not run:
                raise FetchError("The fetch service took too long.")
            items = list(self._client.dataset(run["defaultDatasetId"]).iterate_items())
        except FetchError:
            raise
        except Exception as exc:  # network, quota, bad token: name the error type, never echo details
            raise FetchError("The fetch service failed (%s)." % type(exc).__name__)

        reviews = [review_from_item(i) for i in items if isinstance(i, dict)]
        reviews = [r for r in reviews if r.text][:limit]
        if not reviews:
            raise FetchError("No reviews came back. This service often cannot collect Shopee reviews.")
        return reviews
