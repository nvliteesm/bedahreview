"""Find products that compete with a seller's product, through paid Apify search actors (demo only).

Only Tokopedia and Lazada are searched: both were tested on 2026-10-27 (about $0.02 for 10 results, 5-33 seconds).
TikTok Shop has a search actor in the Store but it is untested here, so a TikTok Shop seller pastes competitor links.
A result is a listing (title, price, shop), never a person. Platform terms are still unverified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .base import FetchError

TOKOPEDIA_SEARCH_ACTOR = "memo23/tokopedia-scraper"
LAZADA_SEARCH_ACTOR = "lergassy/lazada-scraper"
SEARCH_PLATFORMS = ("tokopedia", "lazada")
SEARCH_RESULTS = 10  # listings asked for; the spare ones cover the seller's own listing and repeats from one shop
_LAZADA_ITEM = re.compile(r"-i(\d+)(?:-s\d+)?\.html")


@dataclass(frozen=True)
class Candidate:
    """One competing listing. Deliberately holds no buyer data."""

    platform: str
    title: str
    url: str
    price: Optional[int]
    rating: Optional[float]
    sold: Optional[int]
    shop: str


def _int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _rating(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 5 else None


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def tokopedia_candidate(row: Dict[str, Any]) -> Optional[Candidate]:
    url, title = _text(row.get("productUrl")), _text(row.get("productName"))
    if not url or not title:
        return None
    return Candidate("tokopedia", title, url, _int(row.get("price")), _rating(row.get("rating")), _int(row.get("soldCount")), _text(row.get("shopName")))


def lazada_candidate(row: Dict[str, Any]) -> Optional[Candidate]:
    url, title = _text(row.get("url")), _text(row.get("title"))
    if row.get("type") != "product" or not url or not title:  # the actor adds a free "notice" row to every run
        return None
    return Candidate("lazada", title, url, _int(row.get("price")), _rating(row.get("rating")), _int(row.get("soldQuantity")), _text(row.get("sellerName")))


def search_products(actors: Any, platform: str, query: str) -> List[Candidate]:
    """Listings for a search phrase on one platform, best-reviewed first where the platform allows it. `actors` has run(actor_id, input)."""
    query = _text(query)
    if not query:
        raise FetchError("There is nothing to search for.", code="no_title")
    if platform == "tokopedia":
        rows = actors.run(TOKOPEDIA_SEARCH_ACTOR, {"outputMode": "products", "keywords": [query], "sortBy": "reviews", "maxItems": SEARCH_RESULTS})
        found = [tokopedia_candidate(r) for r in rows]
    elif platform == "lazada":
        rows = actors.run(LAZADA_SEARCH_ACTOR, {"mode": "search", "country": "ID", "searchTerms": [query], "maxItems": SEARCH_RESULTS})
        found = [lazada_candidate(r) for r in rows]
    else:
        raise FetchError("Finding competitors is not available on this platform yet. Paste competitor links instead.", code="search_platform")
    return [c for c in found if c]


def own_key(platform: str, url: str) -> str:
    """What identifies the seller's own listing: the shop name in a Tokopedia link, the item number in a Lazada link."""
    if platform == "lazada":
        match = _LAZADA_ITEM.search(urlsplit(url).path)
        return match.group(1) if match else ""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[0].lower() if parts else ""


def shortlist(candidates: List[Candidate], platform: str, own_url: str) -> List[Candidate]:
    """Drop the seller's own listing (and, on Tokopedia, anything from their shop), then keep one listing per shop."""
    mine = own_key(platform, own_url)
    kept: List[Candidate] = []
    shops = set()
    for cand in candidates:
        key = own_key(platform, cand.url)
        if mine and key == mine:
            continue
        shop = cand.shop.lower() or key
        if shop in shops:
            continue
        shops.add(shop)
        kept.append(cand)
    return kept
