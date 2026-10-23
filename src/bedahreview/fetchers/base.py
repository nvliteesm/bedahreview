"""Shared pieces for fetching reviews from a product link (demo feature)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

MAX_LIMIT = 20  # v1 cap per fetch; the paid actor charges per review

COMPLAINT_STARS = (1, 2, 3)  # "complaints first" fetches these ratings, worst first
MODES = ("complaints", "latest")

_SHOPEE_HOSTS = ("shopee.co.id", "www.shopee.co.id")
_PRODUCT_PATH = re.compile(r"-i\.\d+\.\d+/?$")


class FetchError(Exception):
    """Reviews could not be fetched. The message is safe to show the seller."""


class FetcherConfigError(Exception):
    """The fetcher is switched on in .env but not set up correctly."""


@dataclass(frozen=True)
class Review:
    """One fetched review. Deliberately has no reviewer name, id or avatar, so none can be stored."""

    star: Optional[int]
    text: str
    variant: Optional[str]
    date: str
    photo_count: int
    seller_reply: Optional[str]


class Fetcher:
    """Interface: a product link in, a list of Review out. Raise FetchError on any failure."""

    def fetch_reviews(self, url: str, limit: int, mode: str = "latest") -> List[Review]:
        """mode "complaints" returns 1-3 star reviews (worst first); "latest" returns the newest reviews with text."""
        raise NotImplementedError


def check_shopee_product_url(url: str) -> str:
    """Return the link reduced to https://<host>/<path>, or raise FetchError if it is not a Shopee Indonesia product link."""
    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower()
        has_userinfo = parts.username is not None or parts.password is not None
        port = parts.port
    except ValueError:
        raise FetchError("That does not look like a link.")
    if parts.scheme != "https" or host not in _SHOPEE_HOSTS or has_userinfo or port not in (None, 443):
        raise FetchError("Only https://shopee.co.id product links work in this demo.")
    if not _PRODUCT_PATH.search(parts.path):
        raise FetchError("Open the product page and copy its link. It should end like ...-i.12345.67890")
    return "https://%s%s" % (host, parts.path)


def clamp_limit(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON allows Infinity
        raise FetchError("The number of reviews must be a whole number.")
    return max(1, min(n, MAX_LIMIT))


def product_title_from_url(url: str) -> str:
    """The listing title Shopee puts in the link ("Kaos-Boxy-Retro-i.1.2" -> "Kaos Boxy Retro"), or "" if there is none."""
    slug = unquote(urlsplit(url).path.strip("/")).rsplit("-i.", 1)[0]
    return " ".join(slug.replace("-", " ").split())[:150]


def check_mode(mode: Any) -> str:
    if mode not in MODES:
        raise FetchError("Choose complaints first or latest reviews.")
    return mode


def to_review_dicts(reviews: List[Review]) -> List[Dict[str, Any]]:
    """Shape fetched reviews like pasted ones, so the classifier sees no difference."""
    return [
        {"id": str(i), "stars": r.star, "variant": r.variant, "date": r.date, "text": r.text}
        for i, r in enumerate(reviews, start=1)
    ]
