"""Competitor analysis, step 1: suggest comparable products for the seller to confirm with one tap.

The seller's own link gives a platform and a listing title. We turn the title into a short search phrase, search the
same platform, drop the seller's own listing and repeats from one shop, and let the model pick the closest few from
their titles. Nothing is picked silently: the seller sees every card and can untick one or paste a link instead.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from .classifier import _call, classify, make_client, resolve_model
from .fetchers.base import FetchError, Review, to_review_dicts
from .fetchers.search import Candidate, shortlist

MAX_PICKED = 3
MAX_CONSIDERED = 8  # listings whose titles the model reads
QUERY_WORDS = 6  # the plain-title fallback search phrase
MAX_COMPETITORS = 3
COMPLAINTS_PER_SHOP = 4  # the 1-3 star reviews, worst first
LATEST_PER_SHOP = 3  # plus the newest reviews, so the sample is a mix and not only complaints
MAX_ROWS = 6  # complaint themes shown in the comparison table
MAX_PRAISE_ROWS = 3
GAP = 2  # how many more complaints than the best rival before we say so
NO_THEME = ("No text", "Other")
MIN_CREDIBLE = 5  # a shop with fewer credible reviews than this is shown but never used for a statement

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


# -- step 2: compare ---------------------------------------------------------

def tally(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts for one shop: complaints and praise by theme. Likely-fake reviews are left out of every count."""
    fake = sum(1 for i in items if i["fake"])
    complaints: Dict[str, int] = {}
    praise: Dict[str, int] = {}
    for item in items:
        if item["fake"] or item["theme"] in NO_THEME:
            continue
        if item["theme"].startswith("Praise:"):
            praise[item["theme"]] = praise.get(item["theme"], 0) + 1
        elif item["bucket"] in ("fix", "fight", "check"):
            complaints[item["theme"]] = complaints.get(item["theme"], 0) + 1
    return {"reviews": len(items), "credible": len(items) - fake, "fake": fake, "complaints": complaints, "praise": praise}


def fetch_sample(fetch: Callable[[str, int, str], List[Review]], url: str) -> List[Review]:
    """A mix for one competitor: its worst reviews and its newest, without repeats. Raises FetchError if neither comes back."""
    errors: Dict[str, FetchError] = {}

    def attempt(limit: int, mode: str) -> List[Review]:
        try:
            return fetch(url, limit, mode)
        except FetchError as exc:
            errors[mode] = exc
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        bad = pool.submit(attempt, COMPLAINTS_PER_SHOP, "complaints")
        good = pool.submit(attempt, LATEST_PER_SHOP, "latest")
        found = bad.result() + good.result()
    seen, mixed = set(), []
    for review in found:
        key = (review.star, review.text)
        if key not in seen:
            seen.add(key)
            mixed.append(review)
    if not mixed:
        # The newest-reviews error comes first: "no complaints" is normal for a good product, so it explains little.
        raise errors.get("latest") or errors.get("complaints") or FetchError("No reviews with a comment came back.")
    return mixed


def _quotes(results: List[Dict[str, Any]], by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """One exact quote per theme, to back the counts. The model's quote is already checked against the review text."""
    quotes: Dict[str, Dict[str, Any]] = {}
    for result in results:
        if result["likely_fake"]["flag"] or not result.get("proof_quote") or result["theme"] in NO_THEME:
            continue
        if result["theme"] not in quotes:
            quotes[result["theme"]] = {"quote": result["proof_quote"], "stars": by_id[result["id"]]["stars"]}
    return quotes


def analyse_competitor(
    fetch: Callable[[str, int, str], List[Review]], classify_fn: Callable[..., List[Dict[str, Any]]], url: str, title: str
) -> Dict[str, Any]:
    reviews = fetch_sample(fetch, url)
    listing = title or next((r.product for r in reviews if r.product), "") or ""
    dicts = to_review_dicts(reviews)
    results = classify_fn(dicts, product=listing) if listing else classify_fn(dicts)
    by_id = {d["id"]: d for d in dicts}
    items = [{"theme": r["theme"], "bucket": r["bucket"], "fake": r["likely_fake"]["flag"]} for r in results]
    counts = tally(items)
    return dict(counts, title=listing, url=url, quotes=_quotes(results, by_id), small=counts["credible"] < MIN_CREDIBLE)


def comparison_rows(you: Dict[str, Any], rivals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per theme: your count, then each rival's. Biggest complaint themes first, then a few praise themes."""
    def rows(field: str, kind: str, cap: int) -> List[Dict[str, Any]]:
        themes = set(you[field]).union(*(r[field] for r in rivals))
        built = [{"theme": t, "kind": kind, "you": you[field].get(t, 0), "others": [r[field].get(t, 0) for r in rivals]} for t in themes]
        built.sort(key=lambda r: (-(r["you"] + sum(r["others"])), r["theme"]))
        return built[:cap]
    return rows("complaints", "complaint", MAX_ROWS) + rows("praise", "praise", MAX_PRAISE_ROWS)


def insights(rows: List[Dict[str, Any]], rivals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Plain statements the numbers support, with no model involved: where you trail, and where a rival is weak."""
    found: List[Dict[str, Any]] = []
    usable = [i for i, r in enumerate(rivals) if not r.get("small")]
    if not usable:
        return found
    for row in rows:
        counts = [row["others"][i] for i in usable]
        best, worst = min(counts), max(counts)
        if row["kind"] == "complaint" and row["you"] - best >= GAP:
            found.append({"kind": "worse", "theme": row["theme"], "you": row["you"], "best": best})
        elif row["kind"] == "complaint" and worst - row["you"] >= GAP:
            found.append({"kind": "opportunity", "theme": row["theme"], "you": row["you"], "them": worst, "who": rivals[usable[counts.index(worst)]]["title"]})
        elif row["kind"] == "praise" and worst - row["you"] >= GAP:
            found.append({"kind": "praise_gap", "theme": row["theme"], "you": row["you"], "them": worst, "who": rivals[usable[counts.index(worst)]]["title"]})
    return found


def compare_competitors(
    fetch: Callable[[str, int, str], List[Review]],
    classify_fn: Callable[..., List[Dict[str, Any]]],
    own_items: List[Dict[str, Any]],
    own_title: str,
    competitors: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Fetch a small mix of reviews from each confirmed competitor (never cached, never stored), run the same triage on them, and set the counts beside the seller's own."""
    chosen = competitors[:MAX_COMPETITORS]

    def one(entry: Dict[str, str]) -> Dict[str, Any]:
        try:
            return analyse_competitor(fetch, classify_fn, entry["url"], entry.get("title", ""))
        except FetchError as exc:
            return {"failed": True, "title": entry.get("title", ""), "url": entry["url"], "error": str(exc)}

    with ThreadPoolExecutor(max_workers=len(chosen) or 1) as pool:
        done = list(pool.map(one, chosen))
    rivals = [d for d in done if not d.get("failed")]
    failed = [{"title": d["title"], "url": d["url"], "error": d["error"]} for d in done if d.get("failed")]
    if not rivals:
        raise FetchError(failed[0]["error"] if failed else "No competitors to compare.")
    you = dict(tally(own_items), title=own_title)
    rows = comparison_rows(you, rivals)
    return {"you": you, "competitors": rivals, "failed": failed, "rows": rows, "insights": insights(rows, rivals)}
