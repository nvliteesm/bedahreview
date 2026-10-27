"""Competitor analysis, step 1: suggest comparable products for the seller to confirm with one tap.

The seller's own link gives a platform and a listing title. We turn the title into a short search phrase, search the
same platform, drop the seller's own listing and repeats from one shop, and let the model pick the closest few from
their titles. Nothing is picked silently: the seller sees every card and can untick one or paste a link instead.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from .classifier import _call, make_client, resolve_model
from .fetchers.search import Candidate, shortlist

MAX_PICKED = 3
MAX_CONSIDERED = 8  # listings whose titles the model reads
QUERY_WORDS = 6  # the plain-title fallback search phrase

QUERY_SYSTEM = """You read one marketplace listing title from an Indonesian shop (Tokopedia, Lazada or TikTok Shop). Titles are often stuffed with keywords, promo words and the shop name.
Return one JSON object: {"product_type": "...", "query": "..."}
- product_type: what the product is, 2-4 words, in Bahasa Indonesia (for example "kaos oversize pria", "tas selempang wanita").
- query: the search phrase a buyer would type to find the same kind of product, 2-5 words, in Bahasa Indonesia. No brand, no shop name, no promo words (murah, terbaru, premium, COD, original)."""

PICK_SYSTEM = """A seller sells the product named in "own". Below are listings found by searching the same platform.
Choose up to 3 that a buyer would really compare with the seller's product: the same kind of product, similar use, similar price level. Skip bundles, accessories, other categories, and listings that only share a keyword.
Return one JSON object: {"picks": [index, ...]} with the best match first. Return fewer than 3, or an empty list, if fewer are truly comparable. Judge only from the titles and prices given; do not invent facts."""


def _parse_object(raw: str) -> Dict[str, Any]:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in reply")
    parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("reply is not a JSON object")
    return parsed


def plain_query(title: str) -> str:
    """The first few words of a listing title, used when the model's search phrase cannot be read."""
    return " ".join(title.split("|")[0].split()[:QUERY_WORDS])


def plan_search(client: Any, model: str, title: str) -> Tuple[str, str]:
    """(product_type, search phrase) for a listing title."""
    raw = _call(client, model, QUERY_SYSTEM, [{"role": "user", "content": json.dumps({"title": title}, ensure_ascii=False)}])
    try:
        parsed = _parse_object(raw)
        kind, query = str(parsed.get("product_type") or "").strip(), str(parsed.get("query") or "").strip()
    except ValueError:
        kind, query = "", ""
    return kind[:60], query[:80] or plain_query(title)


def pick_comparable(client: Any, model: str, own_title: str, candidates: List[Candidate]) -> List[int]:
    """Positions (into `candidates`) of the closest matches, best first. An unreadable reply falls back to the search order."""
    if not candidates:
        return []
    listing = [{"index": i, "title": c.title, "price": c.price} for i, c in enumerate(candidates)]
    content = json.dumps({"own": own_title, "listings": listing}, ensure_ascii=False)
    raw = _call(client, model, PICK_SYSTEM, [{"role": "user", "content": content}])
    try:
        picks = _parse_object(raw).get("picks")
        if not isinstance(picks, list):
            raise ValueError("picks is not a list")
    except ValueError:
        return list(range(min(MAX_PICKED, len(candidates))))
    chosen: List[int] = []
    for i in picks:
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(candidates) and i not in chosen:
            chosen.append(i)
    return chosen[:MAX_PICKED]


def suggest_competitors(
    search: Callable[[str, str], List[Candidate]],
    platform: str,
    own_url: str,
    own_title: str,
    client: Optional[Any] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Cards for the confirm step: the picked listings first (pre-ticked), then a few others the seller can swap in."""
    client = client or make_client()
    model = resolve_model(model)
    kind, query = plan_search(client, model, own_title)
    found = shortlist(search(platform, query), platform, own_url)[:MAX_CONSIDERED]
    picked = pick_comparable(client, model, own_title, found)
    cards = [dict(asdict(found[i]), picked=True) for i in picked]
    cards += [dict(asdict(c), picked=False) for i, c in enumerate(found) if i not in picked]
    return {"platform": platform, "product_type": kind, "query": query, "candidates": cards, "picked": len(picked)}
