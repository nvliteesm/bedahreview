"""Anonymous usage events: a fixed list, each with fixed fields, so nothing else can be logged.

One JSON line per event goes to stdout, where Cloud Run logging picks it up. There is no IP address, no user agent, no
product name and no review text. The only free text is the optional comment a tester types into the feedback box,
and the page tells them so. See docs/analytics.md.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

MAX_REVIEWS_FIELD = 50  # same ceiling as one analysis
MAX_COMMENT_CHARS = 300
DEVICE_RE = re.compile(r"^[a-z0-9-]{8,40}$")  # a random id kept in the tester's browser

# event -> field -> rule. A rule is ("enum", choices), ("int", low, high) or ("text", max_chars, optional).
EVENTS: Dict[str, Dict[str, tuple]] = {
    "opened": {},
    "analysed": {"reviews": ("int", 0, MAX_REVIEWS_FIELD), "source": ("enum", ("paste", "link")), "lang": ("enum", ("id", "en"))},
    "tab_viewed": {"tab": ("enum", ("fix", "fight", "ignore", "keep", "rivals"))},
    "draft_copied": {"kind": ("enum", ("fix", "side", "chat", "public", "report"))},
    "fix_marked": {},
    "rivals_started": {},
    "feedback": {"answer": ("enum", ("yes", "partly", "no")), "comment": ("text", MAX_COMMENT_CHARS, True)},
}


def _clean_value(rule: tuple, value: Any) -> Any:
    kind = rule[0]
    if kind == "enum" and isinstance(value, str) and value in rule[1]:
        return value
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool) and rule[1] <= value <= rule[2]:
        return value
    if kind == "text" and isinstance(value, str):
        text = " ".join(value.split())
        if len(text) <= rule[1]:
            return text
    raise ValueError("bad value")


def clean_event(body: Any) -> Dict[str, Any]:
    """The event as it will be logged, or ValueError. Unknown events, unknown fields and wrong types are all refused."""
    if not isinstance(body, dict) or set(body) - {"event", "device", "props"}:
        raise ValueError("bad shape")
    name, device, props = body.get("event"), body.get("device"), body.get("props", {})
    if not isinstance(name, str) or name not in EVENTS:
        raise ValueError("unknown event")
    if not isinstance(device, str) or not DEVICE_RE.match(device):
        raise ValueError("bad device id")
    if not isinstance(props, dict):
        raise ValueError("bad props")
    rules = EVENTS[name]
    if set(props) - set(rules):
        raise ValueError("unexpected field")
    clean: Dict[str, Any] = {}
    for field, rule in rules.items():
        if field not in props or (rule[0] == "text" and props[field] in ("", None)):
            if rule[0] == "text" and rule[2]:
                continue
            raise ValueError("missing field")
        clean[field] = _clean_value(rule, props[field])
    return dict(event=name, device=device, **clean)


def log_line(event: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """A single JSON line in the shape Cloud Logging reads (severity and message are special keys there)."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps(dict({"severity": "INFO", "message": "bedahreview_event", "ts": stamp}, **event), ensure_ascii=False, separators=(",", ":"))
