"""Score the classifier against the labelled Review log.

    python -m bedahreview.evaluate eval/data/review_log.csv [--limit N] [--model M]

The CSV is the "Review log" tab exported from the Google Sheet (File > Download > CSV).
Gold labels in the sheet are Yes/Partly/No "fixable" flags, not fix/fight buckets, so
`gold_bucket()` derives a bucket from them. That mapping is a judgement call, kept in one
place so it can be changed.
"""
from __future__ import annotations

import warnings

# Hide the old-Python / LibreSSL notices from Google's libraries (same as `python -W ignore`).
warnings.filterwarnings("ignore")

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .classifier import ROOT, classify, resolve_model
from .env import has_api_key, load_dotenv

BUCKETS = ["fix", "fight", "ignore", "keep"]  # what the sheet can say
PRED_BUCKETS = BUCKETS + ["check"]  # the model may also set a review aside for the seller

# CSV header (lowercased) prefix -> field name
COLUMNS = {
    "#": "id",
    "product id": "product",
    "stars": "stars",
    "review date": "date",
    "variant": "variant",
    "review text": "text",
    "sentiment": "sentiment",
    "theme": "theme",
    "fixable": "fixable",
    "courier": "courier",
    "likely fake": "fake_note",
}


def _field(header: str) -> Optional[str]:
    h = header.strip().lower()
    for prefix, name in COLUMNS.items():
        if h == prefix or (len(prefix) > 1 and h.startswith(prefix)):
            return name
    return None


def load_rows(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        fields = [_field(h) for h in header]
        missing = {"id", "product", "stars", "text", "theme", "fixable", "courier"} - set(fields)
        if missing:
            raise ValueError("CSV is missing columns: %s" % ", ".join(sorted(missing)))
        rows = []
        for record in reader:
            row = {name: (value or "").strip() for name, value in zip(fields, record) if name}
            if row.get("id"):
                rows.append(row)
    return rows


def gold_bucket(row: Dict[str, str]) -> Optional[str]:
    """Derive a fix/fight/ignore/keep bucket from the sheet's labels. None = cannot tell."""
    if not row.get("text"):
        return "ignore"
    if row.get("fake_note", "").lower().startswith("yes"):
        return "fight"
    if row.get("courier", "").lower() in ("maybe", "yes"):
        return "fight"
    if row.get("theme", "").startswith("Praise:"):
        return "keep"
    fixable = row.get("fixable", "").lower()
    if fixable in ("yes", "partly"):
        return "fix"
    if fixable == "no":  # outside the shop's control or subjective taste
        return "fight"
    return None


def gold_fake(row: Dict[str, str]) -> Optional[bool]:
    """True = marked fake, False = not flagged, None = 'Check: ...' (unsure, left out of scoring)."""
    note = row.get("fake_note", "").lower()
    if note.startswith("yes"):
        return True
    if note.startswith("check"):
        return None
    return False


def to_review(row: Dict[str, str]) -> Dict[str, Any]:
    try:
        stars: Any = int(row["stars"])
    except ValueError:
        stars = row["stars"]
    return {
        "id": row["id"],
        "stars": stars,
        "variant": row.get("variant") or None,
        "date": row.get("date", ""),
        "text": row.get("text", ""),
    }


def predict(rows: List[Dict[str, str]], classify_fn: Callable[..., List[Dict[str, Any]]]):
    """Run the classifier one product at a time (fake-review signals work within a product)."""
    by_product: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_product[row["product"]].append(row)
    preds: Dict[str, Dict[str, Any]] = {}
    failures: Dict[str, str] = {}
    for product, group in by_product.items():
        try:
            for result in classify_fn([to_review(r) for r in group]):
                preds[result["id"]] = result
        except Exception as exc:  # bad reply, timeout, rate limit: mark this product failed, keep going
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            for r in group:
                failures[r["id"]] = "%s: %s" % (product, reason)
    return preds, failures


def _prf(tp: int, fp: int, fn: int) -> Dict[str, Optional[float]]:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def score(rows: List[Dict[str, str]], preds: Dict[str, Dict[str, Any]], failures: Dict[str, str]) -> Dict[str, Any]:
    confusion: Counter = Counter()
    theme_ok = theme_n = 0
    fake = Counter()
    courier = Counter()
    mismatches = []
    scored = 0
    fix_rows = fix_covered = 0

    for row in rows:
        pred = preds.get(row["id"])
        gold = gold_bucket(row)
        if pred is None or gold is None:
            continue
        scored += 1
        confusion[(gold, pred["bucket"])] += 1
        if gold != pred["bucket"]:
            mismatches.append(
                {
                    "id": row["id"],
                    "gold": gold,
                    "pred": pred["bucket"],
                    "gold_theme": row["theme"],
                    "pred_theme": pred["theme"],
                    "cause": pred["cause"],
                    "text": row["text"][:160],
                }
            )
        if gold == "fix":
            fix_rows += 1
            fix_covered += bool(pred.get("fix_suggestion_id"))
        theme_n += 1
        theme_ok += row["theme"] == pred["theme"]

        gf = gold_fake(row)
        if gf is not None:
            pf = pred["likely_fake"]["flag"]
            fake["tp" if gf and pf else "fp" if pf else "fn" if gf else "tn"] += 1

        gc = row["courier"].lower() in ("maybe", "yes")
        pc = bool(pred["check_first"])
        courier["tp" if gc and pc else "fp" if pc else "fn" if gc else "tn"] += 1

    per_bucket = {}
    for b in BUCKETS:
        tp = confusion[(b, b)]
        fp = sum(confusion[(g, b)] for g in BUCKETS if g != b)
        fn = sum(confusion[(b, p)] for p in PRED_BUCKETS if p != b)
        per_bucket[b] = dict(_prf(tp, fp, fn), support=tp + fn)

    correct = sum(confusion[(b, b)] for b in BUCKETS)
    return {
        "reviews": len(rows),
        "scored": scored,
        "failed": len(failures),
        "bucket_accuracy": correct / scored if scored else None,
        "per_bucket": per_bucket,
        "confusion": {g: {p: confusion[(g, p)] for p in PRED_BUCKETS} for g in BUCKETS},
        "set_aside_as_check": {g: confusion[(g, "check")] for g in BUCKETS if confusion[(g, "check")]},
        "accuracy_when_committed": (
            correct / (scored - sum(confusion[(g, "check")] for g in BUCKETS)) if scored > sum(confusion[(g, "check")] for g in BUCKETS) else None
        ),
        "theme_accuracy": theme_ok / theme_n if theme_n else None,
        "fix_suggestion_coverage": fix_covered / fix_rows if fix_rows else None,
        "likely_fake": _prf(fake["tp"], fake["fp"], fake["fn"]),
        "check_first_vs_courier": _prf(courier["tp"], courier["fp"], courier["fn"]),
        "mismatches": mismatches,
        "failures": failures,
    }


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else "%.0f%%" % (100 * x)


def format_report(m: Dict[str, Any], max_mismatches: int = 15) -> str:
    lines = [
        "Model: %s" % m.get("model", "unknown"),
        "Scored %d of %d reviews (%d failed to classify)" % (m["scored"], m["reviews"], m["failed"]),
        "Bucket accuracy: %s    Theme accuracy: %s" % (_pct(m["bucket_accuracy"]), _pct(m["theme_accuracy"])),
        "Sheet-fixable reviews that got a fix suggestion (any bucket): %s" % _pct(m["fix_suggestion_coverage"]),
        "",
        "Per bucket        precision  recall  support",
    ]
    for b in BUCKETS:
        s = m["per_bucket"][b]
        lines.append("  %-14s %8s %7s %8d" % (b, _pct(s["precision"]), _pct(s["recall"]), s["support"]))
    lines += ["", "Confusion (rows = sheet label, columns = model)", "  %-8s" % "" + "".join("%8s" % b for b in PRED_BUCKETS)]
    for g in BUCKETS:
        lines.append("  %-8s" % g + "".join("%8d" % m["confusion"][g][p] for p in PRED_BUCKETS))
    aside = m["set_aside_as_check"]
    lines.append(
        "Set aside as 'check': %d of %d (sheet says: %s). Accuracy on the ones the model committed to: %s"
        % (sum(aside.values()), m["scored"], ", ".join("%s %d" % kv for kv in aside.items()) or "none", _pct(m["accuracy_when_committed"]))
    )
    f, c = m["likely_fake"], m["check_first_vs_courier"]
    lines += [
        "",
        "Likely-fake flag:   precision %s  recall %s  (tp %d, fp %d, fn %d)"
        % (_pct(f["precision"]), _pct(f["recall"]), f["tp"], f["fp"], f["fn"]),
        "Courier check-first: precision %s  recall %s  (tp %d, fp %d, fn %d)"
        % (_pct(c["precision"]), _pct(c["recall"]), c["tp"], c["fp"], c["fn"]),
    ]
    if m["mismatches"]:
        lines += ["", "Disagreements (first %d of %d):" % (min(max_mismatches, len(m["mismatches"])), len(m["mismatches"]))]
        for x in m["mismatches"][:max_mismatches]:
            lines.append("  #%s sheet=%s model=%s (%s) | %s" % (x["id"], x["gold"], x["pred"], x["pred_theme"], x["text"]))
    if m["failures"]:
        lines += ["", "Failed:"] + ["  #%s %s" % kv for kv in list(m["failures"].items())[:10]]
    lines += [
        "",
        "Note: the sheet's theme/fixable/courier labels are drafts and the fix/fight bucket is derived from",
        "them (see gold_bucket). This measures agreement with the sheet, not ground truth.",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score the classifier against the labelled Review log CSV.")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--limit", type=int, help="only the first N reviews (cheap smoke test)")
    parser.add_argument("--model", help="override the Gemini model")
    args = parser.parse_args(argv)

    load_dotenv()
    if not has_api_key():
        print("GEMINI_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1
    if not args.csv.exists():
        print(
            "%s not found. Export the 'Review log' tab (File > Download > CSV) to that path." % args.csv,
            file=sys.stderr,
        )
        return 1
    rows = load_rows(args.csv)
    if args.limit:
        rows = rows[: args.limit]
    print("Classifying %d reviews in %d product group(s)..." % (len(rows), len({r["product"] for r in rows})))
    preds, failures = predict(rows, lambda reviews: classify(reviews, model=args.model))
    metrics = score(rows, preds, failures)
    metrics["model"] = resolve_model(args.model)
    print(format_report(metrics))

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    out.write_text(json.dumps({"metrics": metrics, "predictions": preds}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nFull results: %s" % out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
