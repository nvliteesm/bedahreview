"""Turn the text a seller pastes into review dicts for classify().

Accepted shapes, mixed freely:

    ★★★☆☆  Putih, M   warna tulisannya ijo, pas dateng hitem      (stars, variant, text)
    4★ bahannya tebel                                              (stars, text)
    Pas sampe koyak bajunya                                        (text only)

If the paste contains blank lines, a blank line separates reviews (so a review may span several
lines). Otherwise every line is one review. Stars are only read when written as ★ glyphs, "4★",
"4/5" or "bintang 4", so a review that merely starts with a number ("3 hari rusak") is left alone.
A variant is only read after stars and must contain a comma ("Putih, L") or be followed by " | ".
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_GLYPHS = re.compile(r"^\s*([★☆]{1,5})\s*[:\-–|]?\s*")
_NUMERIC = re.compile(r"^\s*(?:([1-5])\s*(?:/\s*5|★|\*)|bintang\s*([1-5]))\s*[:\-–|]?\s*", re.IGNORECASE)
_VARIANT = re.compile(r"^(?P<variant>[^\t|]{1,40}?)(?:\t+|\s{2,}|\s\|\s)(?P<text>\S.*)$", re.DOTALL)


def _split_reviews(raw: str) -> List[str]:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    parts = re.split(r"\n\s*\n", raw) if re.search(r"\n\s*\n", raw) else raw.split("\n")
    return [p.strip() for p in parts if p.strip()]


def _read_stars(chunk: str):
    m = _GLYPHS.match(chunk)
    if m and "★" in m.group(1):
        return m.group(1).count("★"), chunk[m.end() :]
    m = _NUMERIC.match(chunk)
    if m:
        return int(m.group(1) or m.group(2)), chunk[m.end() :]
    return None, chunk


def parse_reviews(raw: str) -> List[Dict[str, Any]]:
    reviews: List[Dict[str, Any]] = []
    for chunk in _split_reviews(raw):
        stars, rest = _read_stars(chunk)
        variant = None
        if stars is not None:
            rest = rest.strip()
            m = _VARIANT.match(rest)
            if m and ("," in m.group("variant") or "|" in rest[m.end("variant") : m.end("variant") + 3]):
                variant, rest = m.group("variant").strip(), m.group("text")
        reviews.append(
            {"id": str(len(reviews) + 1), "stars": stars, "variant": variant, "date": "", "text": rest.strip()}
        )
    return reviews
