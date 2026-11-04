"""Find products that compete with a seller's product, through paid Apify search actors (demo only).

Tokopedia and Lazada were tested on 2026-10-27 (about $0.02 for 10 results, 5-33 seconds; Lazada links are refused while the Lazada switch is off).
TikTok Shop was tested on 2026-11-04 with cunning_soil's search actor: Indonesia works on the free Apify plan, 6 listings for one search page, $0.10.
(lergassy's TikTok Shop actor was tried first: on the free plan it only returns what someone else asked for that day, so it gave nothing.)
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
TIKTOK_SEARCH_ACTOR = "cunning_soil/tiktok-shop-product-search-api"  # $0.10 for one search page of up to 10 listings; the spend cap for it is in platforms.ACTOR_MIN_CAP_USD
SEARCH_PLATFORMS = ("tokopedia", "lazada", "tiktokshop")
SEARCH_RESULTS = 10  # listings asked for; the spare ones cover the seller's own listing and repeats from one shop
TIKTOK_PRODUCT_URL = "https://shop-id.tokopedia.com/view/product/%s"  # the one link form identify_link produces for a TikTok Shop product
_LAZADA_ITEM = re.compile(r"-i(\d+)(?:-s\d+)?\.html")
_TIKTOK_ITEM = re.compile(r"/view/product/(\d{15,})")


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


def _rupiah(value: Any) -> Optional[int]:
    """TikTok Shop prices come as text with dots for thousands: "37.000" is Rp 37,000."""
    digits = re.sub(r"\D", "", value) if isinstance(value, str) else ""
    return int(digits) if digits and int(digits) > 0 else None


def tiktok_candidate(row: Dict[str, Any]) -> Optional[Candidate]:
    pid, title = _text(row.get("product_id")), _text(row.get("title"))
    if not re.fullmatch(r"\d{15,}", pid) or not title:
        return None
    price = row.get("price") if isinstance(row.get("price"), dict) else {}
    store = row.get("store_info") if isinstance(row.get("store_info"), dict) else {}
    return Candidate("tiktokshop", title, TIKTOK_PRODUCT_URL % pid, _rupiah(price.get("sale_price")), _rating(row.get("rating")), _int(row.get("sales_count")), _text(store.get("name")))


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
    elif platform == "tiktokshop":
        rows = actors.run(TIKTOK_SEARCH_ACTOR, {"query": query, "region": "ID", "resultsLimit": SEARCH_RESULTS, "sortType": "BEST_SELLERS"})
        found = [tiktok_candidate(r) for r in rows]
    else:
        raise FetchError("Finding competitors is not available on this platform yet. Paste competitor links instead.", code="search_platform")
    return [c for c in found if c]


def own_key(platform: str, url: str) -> str:
    """What identifies the seller's own listing: the shop name in a Tokopedia link, the item number in a Lazada or TikTok Shop link."""
    if platform == "lazada":
        match = _LAZADA_ITEM.search(urlsplit(url).path)
        return match.group(1) if match else ""
    if platform == "tiktokshop":
        match = _TIKTOK_ITEM.search(urlsplit(url).path)
        return match.group(1) if match else ""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[0].lower() if parts else ""


def shortlist(candidates: List[Candidate], platform: str, own_url: str) -> List[Candidate]:
    """Drop the seller's own listing and anything from their shop, then keep one listing per shop.
    A Tokopedia link names the shop. A Lazada or TikTok Shop link does not, so when the search lists the seller's own product, the shop it shows is theirs."""
    mine = own_key(platform, own_url)
    own_shops = {c.shop.lower() for c in candidates if mine and c.shop and own_key(platform, c.url) == mine}
    kept: List[Candidate] = []
    shops = set()
    for cand in candidates:
        key = own_key(platform, cand.url)
        if mine and key == mine:
            continue
        shop = cand.shop.lower() or key
        if shop in own_shops:
            continue
        if shop in shops:
            continue
        shops.add(shop)
        kept.append(cand)
    return kept
