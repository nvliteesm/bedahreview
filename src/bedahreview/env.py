"""Tiny .env loader so scripts can read GEMINI_API_KEY without another dependency."""
from __future__ import annotations

import os
import re

from .classifier import ROOT


def _clean(value: str) -> str:
    value = value.strip()
    if value[:1] in ("'", '"') and value.count(value[0]) >= 2:
        return value[1 : value.index(value[0], 1)]  # quoted: everything inside, even a "#"
    match = re.search(r"\s#", value)
    return (value[: match.start()] if match else value).strip()  # unquoted: " # note" is a comment


def load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), _clean(value))


def has_api_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
