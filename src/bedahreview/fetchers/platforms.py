"""Fetch reviews from a Tokopedia, Lazada or TikTok Shop product link through paid Apify actors (demo only).

The seller pastes one link and never picks a platform: the link decides. Platform terms are not verified.
Nothing here tries to get around blocks, logins or captchas. Short links are followed only through plain
redirects, and only between known shop hosts. Shopee Indonesia is not served: its review feed is closed to
outside tools (checked 2026-10-26), so a Shopee link gets a clear message and costs nothing.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .apify_shopee import _per_star_limit
from .budget import DailyBudget
from .base import COMPLAINT_STARS, Fetcher, FetchError, LinkError, Review, check_mode, clamp_limit
from .cache import DEFAULT_TTL_HOURS, CachedFetcher
from .search import Candidate, search_products

TOKOPEDIA_ACTOR = "shahidirfan/Tokopedia-Reviews-Scraper"
LAZADA_ACTOR = "zen-studio/lazada-reviews-scraper"
TIKTOK_ACTOR = "web_wanderer/tiktok-reviews-scraper"
RUN_TIMEOUT_SECS = 90  # the three actors finished in 9-14 seconds when tested on 2026-10-26
SPARE_REVIEWS = 3  # ask for a few more than needed: some reviews have no text and are dropped
MAX_CONCURRENT_RUNS = 3  # Apify refused the 6th of 6 simultaneous runs on 2026-10-27, so wait our turn instead
_RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)
START_RETRY_DELAY_S = 4  # one retry when Apify's API rejects a run before it starts (busy, rate limit); such a run is never billed

TOKOPEDIA_HOSTS = ("tokopedia.com", "www.tokopedia.com")
TIKTOK_SHOP_HOST = "shop-id.tokopedia.com"  # what a vt.tokopedia.com share link lands on
TIKTOK_HOSTS = ("www.tiktok.com", "tiktok.com")
LAZADA_HOSTS = ("lazada.co.id", "www.lazada.co.id")
SHORT_HOSTS = ("vt.tokopedia.com", "tokopedia.link", "vt.tiktok.com", "vm.tiktok.com")
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_HOPS = 5
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")
_TIKTOK_ID = re.compile(r"/(\d{15,})(?:/|$)")

SUPPORTED = "Tokopedia, Lazada or TikTok Shop"
SHOPEE_MESSAGE = "Shopee Indonesia does not give its reviews to outside tools right now, so a Shopee link cannot be read. Paste the reviews or use Share from the Shopee app instead."


# -- links ----------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # report the redirect instead of following it
        return None


def _allowed_host(host: str) -> bool:
    return host in SHORT_HOSTS or host in TOKOPEDIA_HOSTS or host in LAZADA_HOSTS or host in TIKTOK_HOSTS or host == TIKTOK_SHOP_HOST


def _split(url: str):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        bad = parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.port not in (None, 443)
    except ValueError:
        raise LinkError("That does not look like a link.")
    return parts, host, bad


def resolve_short_link(url: str, opener: Optional[Any] = None) -> str:
    """Follow a share link's redirects by hand, one hop at a time, and only while each hop stays on a known shop host."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(_MAX_HOPS):
        _, host, bad = _split(current)
        if bad or not _allowed_host(host):
            raise LinkError("Could not open that short link. Open the product and copy its full link instead.")
        if host not in SHORT_HOSTS:
            return current
        request = urllib.request.Request(current, headers={"User-Agent": "BedahReview/0.1"})
        try:
            opener.open(request, timeout=8).close()
            raise LinkError("Could not open that short link. Open the product and copy its full link instead.")
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in _REDIRECT_CODES and exc.headers else None
            if not location:
                raise LinkError("Could not open that short link. Open the product and copy its full link instead.")
            current = urljoin(current, location)
        except (urllib.error.URLError, OSError):
            raise LinkError("Could not open that short link. Open the product and copy its full link instead.")
    raise LinkError("Could not open that short link. Open the product and copy its full link instead.")


def identify_link(text: str, resolver: Callable[[str], str] = resolve_short_link) -> Tuple[str, str]:
    """Return (platform, clean link) for a pasted link, or raise LinkError. Platforms: tokopedia, lazada, tiktokshop."""
    found = _URL_IN_TEXT.search(text or "")  # a Share message can be words around the link
    url = (found.group(0) if found else (text or "").strip()).rstrip(".,;)")
    parts, host, bad = _split(url)
    if bad and parts.scheme != "https":
        raise LinkError("Paste the full https link of a %s product." % SUPPORTED)
    if bad:
        raise LinkError("That does not look like a product link.")
    if host in SHORT_HOSTS:
        url = resolver(url)
        parts, host, bad = _split(url)
        if bad or not _allowed_host(host):
            raise LinkError("That short link does not lead to a %s product." % SUPPORTED)
    path = parts.path
    if host == "shopee.co.id" or host.endswith(".shopee.co.id"):
        raise LinkError(SHOPEE_MESSAGE)
    if host in LAZADA_HOSTS:
        if not path.startswith("/products/") or not path.endswith(".html"):
            raise LinkError("Open the Lazada product page and copy its link. It should start with lazada.co.id/products/")
        return "lazada", "https://www.lazada.co.id" + path
    if host == TIKTOK_SHOP_HOST or host in TIKTOK_HOSTS:
        match = _TIKTOK_ID.search(path if path.endswith("/") else path + "/")
        if not match or (host in TIKTOK_HOSTS and "/shop/" not in path):
            raise LinkError("That is not a TikTok Shop product link.")
        return "tiktokshop", "https://%s/view/product/%s" % (TIKTOK_SHOP_HOST, match.group(1))
    if host in TOKOPEDIA_HOSTS:
        if len([s for s in path.split("/") if s]) < 2:
            raise LinkError("Open the Tokopedia product page and copy its link (it has the shop name, then the product).")
        return "tokopedia", "https://www.tokopedia.com" + path
    raise LinkError("This link is not supported. Paste a %s product link, or paste the reviews." % SUPPORTED)


# -- shared pieces ----------------------------------------------------------

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _star(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5 else None


def _count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _replied(value: Any) -> bool:
    """Whether the shop answered. Only yes or no is kept: a reply's text can name the buyer, so it is looked at and thrown away."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(isinstance(value.get(key), str) and value[key].strip() for key in ("comment", "text", "content", "reply"))
    return False


def _clean_variant(value: Any) -> Optional[str]:
    return re.sub(r"\s*,\s*", ", ", value.strip()) or None if isinstance(value, str) else None


def _iso_date(value: Any) -> str:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    return value[:10] if isinstance(value, str) else ""


class _Actors:
    """Runs an Apify actor with a hard spending cap and returns its rows. Errors name a type, never details or the token."""

    def __init__(self, client: Any, max_cost_usd: float, budget: Optional[DailyBudget] = None) -> None:
        self._client, self._cap, self._budget = client, max_cost_usd, budget

    def _start(self, actor_id: str, run_input: Dict[str, Any]) -> Any:
        """Start an actor and wait for it. A start the API itself rejects is tried once more; nothing else is retried, so a run is never paid for twice."""
        for attempt in (1, 2):
            try:
                with _RUN_SLOTS:  # the server handles requests on several threads, and one comparison starts up to six runs
                    return self._client.actor(actor_id).call(
                        run_input=run_input, max_total_charge_usd=Decimal(str(self._cap)), timeout_secs=RUN_TIMEOUT_SECS, wait_secs=RUN_TIMEOUT_SECS
                    )
            except Exception as exc:
                if attempt == 2 or type(exc).__name__ != "ApifyApiError":
                    raise
                time.sleep(START_RETRY_DELAY_S)

    def run(self, actor_id: str, run_input: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            if self._budget:
                self._budget.reserve(self._cap)  # before the run, at its worst-case cost; a cache hit never gets here
            run = self._start(actor_id, run_input)
            if not run:
                raise FetchError("The fetch service took too long.")
            if run.get("status") not in (None, "SUCCEEDED"):
                raise FetchError("The fetch service could not read reviews for that link.")
            items = list(self._client.dataset(run["defaultDatasetId"]).iterate_items())
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError("The fetch service failed (%s)." % type(exc).__name__)
        return [i for i in items if isinstance(i, dict)]


def _finish(reviews: List[Review], limit: int, mode: str) -> List[Review]:
    """Keep reviews with text, the low-rated ones in complaints mode (worst first), and no more than asked for."""
    kept = [r for r in reviews if r.text]
    if mode == "complaints":
        kept = sorted((r for r in kept if r.star is not None and r.star <= max(COMPLAINT_STARS)), key=lambda r: r.star or 0)
        if not kept:
            raise FetchError("No low-rated reviews with a comment were found for that product. Try Latest reviews.")
    if not kept:
        raise FetchError("No reviews with a comment came back for that product.")
    return kept[:limit]


# -- one fetcher per platform -----------------------------------------------

def tokopedia_review(item: Dict[str, Any]) -> Review:
    """Copy only the fields we need. The buyer's name, id and profile link are never read."""
    return Review(
        star=_star(item.get("rating")), text=_text(item.get("review_text")), variant=_clean_variant(item.get("variant_name")),
        date=_iso_date(item.get("review_time_iso")), photo_count=_count(item.get("images_count")),
        replied=_replied(item.get("seller_reply_text")), product=_text(item.get("product_name")) or None,
    )


def lazada_review(item: Dict[str, Any]) -> Optional[Review]:
    review = item.get("review")
    if not isinstance(review, dict):
        return None  # a summary row, not a review
    sku = review.get("sku_info")
    variant = None
    if isinstance(sku, str):
        values = [re.sub(r"^Int:\s*", "", p.split(":", 1)[1] if ":" in p else p).strip() for p in sku.split(", ")]  # "Tampilan Warna:Biru, Ukuran:Int:XXL" -> "Biru, XXL"
        variant = ", ".join(v for v in values if v) or None
    product = item.get("product")
    return Review(
        star=_star(review.get("rating")), text=_text(review.get("content")), variant=variant,
        date=_text(review.get("review_time")), photo_count=_count(review.get("image_count")),
        replied=_replied(item.get("seller_reply")), product=(_text(product.get("title")) if isinstance(product, dict) else "") or None,
    )


def tiktok_review(item: Dict[str, Any]) -> Review:
    return Review(
        star=_star(item.get("review_rating")), text=_text(item.get("review_text")), variant=_clean_variant(item.get("sku_specification")),
        date=_iso_date(item.get("review_time")), photo_count=_count(item.get("review_images")),
        replied=False, product=_text(item.get("product_name")) or None,
    )


class TokopediaFetcher(Fetcher):
    def __init__(self, actors: _Actors) -> None:
        self._actors = actors

    def fetch_reviews(self, url: str, limit: int, mode: str = "latest") -> List[Review]:
        complaints = mode == "complaints"
        rows = self._actors.run(TOKOPEDIA_ACTOR, {
            "product_url": url, "results_wanted": limit + SPARE_REVIEWS, "max_pages": 3,
            "sort_by": "rating asc" if complaints else "create_time desc",
        })
        return _finish([tokopedia_review(r) for r in rows], limit, mode)


class LazadaFetcher(Fetcher):
    def __init__(self, actors: _Actors) -> None:
        self._actors = actors

    def fetch_reviews(self, url: str, limit: int, mode: str = "latest") -> List[Review]:
        run_input: Dict[str, Any] = {"startUrls": [{"url": url}], "maxReviewsPerProduct": limit + SPARE_REVIEWS, "includeRatingSummary": False}
        if mode == "complaints":
            run_input["starRatings"] = [str(s) for s in COMPLAINT_STARS]
        rows = self._actors.run(LAZADA_ACTOR, run_input)
        return _finish([r for r in (lazada_review(i) for i in rows) if r], limit, mode)


class TikTokShopFetcher(Fetcher):
    def __init__(self, actors: _Actors) -> None:
        self._actors = actors

    def _run(self, product_id: str, count: int, reviews_filter: str, sort: str) -> List[Review]:
        rows = self._actors.run(TIKTOK_ACTOR, {
            "region": "ID", "product_ids": [product_id], "reviews_limit": count, "reviews_filter": reviews_filter,
            "reviews_sort": sort, "include_personal_information": False,
        })
        return [tiktok_review(r) for r in rows]

    def fetch_reviews(self, url: str, limit: int, mode: str = "latest") -> List[Review]:
        match = _TIKTOK_ID.search(urlsplit(url).path + "/")
        if not match:
            raise LinkError("That is not a TikTok Shop product link.")
        product_id, want = match.group(1), limit + SPARE_REVIEWS
        if mode != "complaints":
            return _finish(self._run(product_id, want, "all", "most_recent"), limit, mode)
        with ThreadPoolExecutor(max_workers=len(COMPLAINT_STARS)) as pool:  # the actor filters one star at a time
            futures = [pool.submit(self._run, product_id, _per_star_limit(want), "%d_star" % star, "most_recent") for star in COMPLAINT_STARS]
        batches: List[List[Review]] = []
        errors: List[FetchError] = []
        for future in futures:
            try:
                batches.append(future.result())
            except FetchError as exc:
                errors.append(exc)
        if not any(batches) and errors:
            raise errors[0]
        return _finish([r for batch in batches for r in batch], limit, mode)


class LinkRouter(Fetcher):
    """One fetcher for every supported platform: the pasted link picks the actor, and each platform has its own cache."""

    def __init__(self, token: str, max_cost_usd: float, cache_dir: Path, client: Optional[Any] = None, resolver: Callable[[str], str] = resolve_short_link, budget: Optional[DailyBudget] = None, cache_ttl_hours: float = DEFAULT_TTL_HOURS) -> None:
        if client is None:
            from apify_client import ApifyClient

            client = ApifyClient(token)
        actors = _Actors(client, max_cost_usd, budget)
        self._actors = actors
        keep = lambda url: url  # links are checked by check_link before they reach a cache
        self._caches = {
            "tokopedia": CachedFetcher(TokopediaFetcher(actors), cache_dir, keep, cache_ttl_hours),
            "lazada": CachedFetcher(LazadaFetcher(actors), cache_dir, keep, cache_ttl_hours),
            "tiktokshop": CachedFetcher(TikTokShopFetcher(actors), cache_dir, keep, cache_ttl_hours),
        }
        self._resolver = resolver
        self._local = threading.local()  # the server handles requests on several threads

    @property
    def last_from_cache(self) -> bool:
        cache = getattr(self._local, "cache", None)
        return bool(cache and cache.last_from_cache)

    @property
    def last_platform(self) -> Optional[str]:
        return getattr(self._local, "platform", None)

    def check_link(self, text: str) -> str:
        """The clean product link for a pasted link, or LinkError. Follows a share link's redirects (free, no actor run)."""
        platform, url = identify_link(text, self._resolver)
        self._local.platform = platform
        return url

    def fetch_reviews(self, url: str, limit: int, mode: str = "latest") -> List[Review]:
        limit, mode = clamp_limit(limit), check_mode(mode)
        platform, clean = identify_link(url, self._resolver)
        self._local.platform = platform
        self._local.cache = self._caches[platform]
        return self._caches[platform].fetch_reviews(clean, limit, mode)

    def fetch_fresh(self, url: str, limit: int, mode: str = "latest") -> List[Review]:
        """Like fetch_reviews but never reads or writes the disk cache: competitor reviews are not kept."""
        limit, mode = clamp_limit(limit), check_mode(mode)
        platform, clean = identify_link(url, self._resolver)
        return self._caches[platform].inner.fetch_reviews(clean, limit, mode)

    def search(self, platform: str, query: str) -> List[Candidate]:
        """Competing listings for a search phrase (paid actor run, spend-capped, never cached)."""
        return search_products(self._actors, platform, query)
