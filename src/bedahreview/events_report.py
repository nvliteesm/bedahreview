"""Turn the usage-event log lines into a table you can read.

    python -m bedahreview.events_report events.jsonl
    gcloud logging read 'jsonPayload.message="bedahreview_event"' --format=json --freshness=30d | python -m bedahreview.events_report -

Accepts our own JSON lines, or the JSON list that `gcloud logging read --format=json` prints. Lines that are not events are skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

from .events import EVENTS


def parse_events(text: str) -> List[Dict[str, Any]]:
    """Every usage event found in the text. Anything that is not one of ours is ignored."""
    try:
        whole = json.loads(text)
        entries: List[Any] = whole if isinstance(whole, list) else [whole]
    except ValueError:
        entries = []
        for line in text.splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    events = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("jsonPayload"), dict):
            entry = entry["jsonPayload"]  # a Cloud Logging entry: our fields sit inside it
        if isinstance(entry, dict) and entry.get("message") == "bedahreview_event" and entry.get("event") in EVENTS and entry.get("device"):
            events.append(entry)
    return events


def summarise(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    events = list(events)
    per_event = Counter(e["event"] for e in events)
    devices: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"events": 0, "analysed": 0, "copied": 0, "first": "", "last": ""})
    analysed = [e for e in events if e["event"] == "analysed"]
    feedback = [e for e in events if e["event"] == "feedback"]
    for e in events:
        d = devices[e["device"]]
        d["events"] += 1
        d["analysed"] += e["event"] == "analysed"
        d["copied"] += e["event"] == "draft_copied"
        stamp = e.get("ts", "")
        d["first"] = min(filter(None, [d["first"], stamp]), default="")
        d["last"] = max(d["last"], stamp)
    return {
        "total": len(events),
        "devices": len(devices),
        "per_event": dict(per_event),
        "per_device": dict(devices),
        "analysed_by_source": dict(Counter(e["source"] for e in analysed)),
        "analysed_by_lang": dict(Counter(e["lang"] for e in analysed)),
        "avg_reviews": round(sum(e["reviews"] for e in analysed) / len(analysed), 1) if analysed else None,
        "tab_views": dict(Counter(e["tab"] for e in events if e["event"] == "tab_viewed")),
        "drafts_copied": dict(Counter(e["kind"] for e in events if e["event"] == "draft_copied")),
        "feedback": {a: sum(1 for f in feedback if f["answer"] == a) for a in ("yes", "partly", "no")},
        "comments": [(f["answer"], f["comment"]) for f in feedback if f.get("comment")],
        "devices_that_analysed": sum(1 for d in devices.values() if d["analysed"]),
        "devices_that_copied": sum(1 for d in devices.values() if d["copied"]),
    }


def _table(title: str, rows: List[tuple]) -> List[str]:
    if not rows:
        return [title, "  (none)", ""]
    width = max(len(str(r[0])) for r in rows)
    return [title] + ["  %-*s  %s" % (width, r[0], r[1]) for r in rows] + [""]


def format_report(s: Dict[str, Any]) -> str:
    if not s["total"]:
        return "No usage events found.\n"
    fb = s["feedback"]
    answered = sum(fb.values())
    lines = ["%d events from %d devices (%d analysed at least once, %d copied a draft)." % (s["total"], s["devices"], s["devices_that_analysed"], s["devices_that_copied"]), ""]
    lines += _table("Events", sorted(s["per_event"].items(), key=lambda kv: -kv[1]))
    lines += _table("Analyses by source", sorted(s["analysed_by_source"].items())) + _table("Analyses by language", sorted(s["analysed_by_lang"].items()))
    if s["avg_reviews"] is not None:
        lines += ["Average reviews per analysis: %s" % s["avg_reviews"], ""]
    lines += _table("Tabs viewed", sorted(s["tab_views"].items(), key=lambda kv: -kv[1])) + _table("Drafts copied", sorted(s["drafts_copied"].items(), key=lambda kv: -kv[1]))
    lines += ["Was this useful? (%d answers)" % answered] + ["  %-7s %d%s" % (a, fb[a], " (%.0f%%)" % (100 * fb[a] / answered) if answered else "") for a in ("yes", "partly", "no")] + [""]
    lines += _table("Per device", [(d[:8] + "…", "%d events, %d analyses, %d drafts copied, %s to %s" % (v["events"], v["analysed"], v["copied"], v["first"][:10], v["last"][:10]))
                                   for d, v in sorted(s["per_device"].items(), key=lambda kv: -kv[1]["events"])])
    if s["comments"]:
        lines += ["Comments (typed by testers)"] + ["  [%s] %s" % c for c in s["comments"]] + [""]
    lines.append("Counts are events, not people: a device is one browser. Few testers means these are stories, not statistics.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise BedahReview usage events.")
    parser.add_argument("log", help="a file with the log lines, or - for standard input")
    args = parser.parse_args(argv)
    text = sys.stdin.read() if args.log == "-" else open(args.log, encoding="utf-8").read()
    sys.stdout.write(format_report(summarise(parse_events(text))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
