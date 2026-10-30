"""Anonymous usage events: a fixed list, each with fixed fields, so nothing else can be logged.

One JSON line per event goes to stdout, where Cloud Run logging picks it up. There is no IP address, no user agent, no
product name and no review text. The only free text is the optional comment a tester types into the feedback box,
and the page tells them so. Two more events (SERVER_EVENTS) are written by the server itself. See docs/analytics.md.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

MAX_REVIEWS_FIELD = 50  # same ceiling as one analysis
MAX_COMMENT_CHARS = 300
MAX_BATCH = 50  # events in one request: the page sends what it has queued every 10 seconds, or when the page is hidden
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
    "free_unlocked": {},  # signed in to see the details of the free analysis made before having an account
}

# Written by the server itself when an analysis fails or is refused by a limit, so the owner's dashboard can show what goes wrong.
# The page can never send these. `code` is one of the server's own error codes (a short slug), never a message.
STEPS = ("analyse", "suggest", "compare")
LIMITS = ("daily", "hour", "budget", "busy")
CODE_RE = re.compile(r"^[a-z_]{1,30}$")
SERVER_EVENTS: Dict[str, tuple] = {"analysis_failed": ("step", "code"), "limit_hit": ("step", "limit")}  # event -> its fields
SERVER_DEVICE = "server"  # stands where a browser's device id would be in the log line


def server_event(name: str, step: str, value: str) -> Dict[str, Any]:
    """One of SERVER_EVENTS, ready for log_line and db_row. A value outside the fixed choices becomes "other"."""
    if name == "analysis_failed":
        return {"event": name, "device": SERVER_DEVICE, "step": step if step in STEPS else "analyse", "code": value if CODE_RE.match(value or "") else "other"}
    if name == "limit_hit":
        return {"event": name, "device": SERVER_DEVICE, "step": step if step in STEPS else "analyse", "limit": value if value in LIMITS else "hour"}
    raise ValueError("unknown server event")


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


def clean_batch(body: Any) -> tuple:
    """(device, [event, ...]) for a batch from the page, or ValueError. One bad event refuses the whole batch: the page is ours, so a bad one is a bug worth seeing."""
    if not isinstance(body, dict) or set(body) - {"device", "events"}:
        raise ValueError("bad shape")
    items = body.get("events")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH:
        raise ValueError("bad batch")
    events = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"event", "props"}:
            raise ValueError("bad event")
        events.append(clean_event({"event": item.get("event"), "device": body.get("device"), "props": item.get("props", {})}))
    return body["device"], events


def db_row(user_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """The row saved in Supabase: the account, the event name and its fixed fields. No device id, no address, no text beyond the optional feedback comment."""
    return {"user_id": user_id, "event": event["event"], "props": {k: v for k, v in event.items() if k not in ("event", "device")}}
