"""classify(): send pasted reviews to Gemini with the triage prompt and return validated results."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "prompts" / "classifier.md"
SCHEMA_PATH = ROOT / "prompts" / "output_schema.json"

DEFAULT_MODEL = "gemini-3.5-flash-lite"
BATCH_SIZE = 15  # reviews per API call; fake-review signals only work inside one batch
REQUEST_TIMEOUT_MS = 180_000  # SDK-level timeout; it did not stop a hung connection in practice
REQUEST_DEADLINE_S = 120  # hard wall-clock cap per attempt, enforced by us
RETRY_DELAYS_S = (2, 6)  # waits before retrying a temporary API error (503, 429, timeout)
MAX_TOKENS = 16000  # Gemini counts its internal thinking tokens against this too


class ClassificationError(Exception):
    """The model's reply could not be turned into valid triage results."""


def _load_schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _extract_json_array(raw: str) -> List[Any]:
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array found in reply")
    parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("reply is not a JSON array")
    return parsed


_BAD_QUOTE = "proof_quote is not an exact substring of the review text"


def no_text_result(review: Dict[str, Any]) -> Dict[str, Any]:
    """The Ignore result for a star rating with no text. Built in code: such a review is counted, never analysed."""
    return {
        "id": str(review["id"]), "bucket": "ignore", "theme": "No text", "variant": review.get("variant") or None,
        "cause": "none", "check_first": False, "proof_quote": None, "likely_fake": {"flag": False, "signals": []},
        "fix_suggestion_id": None, "fight": None, "confidence": "high",
    }


def validate_results(reviews: List[Dict[str, Any]], results: List[Any]) -> List[str]:
    """Return a list of problems; empty means the results are usable."""
    validator = Draft202012Validator(_load_schema())
    problems: List[str] = []

    if len(results) != len(reviews):
        problems.append("expected %d results, got %d" % (len(reviews), len(results)))
        return problems

    for review, result in zip(reviews, results):
        rid = str(review["id"])
        errors = sorted(validator.iter_errors(result), key=lambda e: list(e.path))
        if errors:
            problems.extend(
                "review %s: %s%s" % (rid, "/".join(map(str, e.path)) + ": " if e.path else "", e.message)
                for e in errors
            )
            continue
        if result["id"] != rid:
            problems.append("review %s: result id is %r, results must stay in input order" % (rid, result["id"]))
        quote, text = result["proof_quote"], review.get("text") or ""
        if quote is not None and quote not in text:
            problems.append("review %s: %s" % (rid, _BAD_QUOTE))
        if quote is None and text.strip():
            problems.append("review %s: proof_quote is null but the review has text" % rid)
        bucket = result["bucket"]
        if result["likely_fake"]["flag"]:
            if bucket != "fight":
                problems.append("review %s: likely_fake.flag is true, so bucket must be fight (not %s)" % (rid, bucket))
            elif not (result.get("fight") or {}).get("report_text"):
                problems.append("review %s: likely_fake.flag is true, so fight.report_text must be filled" % rid)
        if result["check_first"] and result["cause"] not in ("courier", "unclear"):
            problems.append(
                "review %s: check_first is only for courier-vs-product doubt (cause courier or unclear), not cause %s"
                % (rid, result["cause"])
            )
        if (bucket in ("fight", "check")) != (result.get("fight") is not None):
            problems.append("review %s: 'fight' (the reply kit) must be set exactly when bucket is fight or check" % rid)
        has_fix_hint = bool(result.get("fix_suggestion_id"))
        if bucket == "fix" and not has_fix_hint:
            problems.append("review %s: 'fix_suggestion_id' must be set when bucket is fix" % rid)
        if bucket in ("fight", "keep", "ignore") and has_fix_hint:
            problems.append("review %s: 'fix_suggestion_id' must be null when bucket is %s" % (rid, bucket))
    return problems


def make_client() -> Any:
    from google import genai
    from google.genai import types

    # reads GEMINI_API_KEY (or GOOGLE_API_KEY) from the environment
    return genai.Client(http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))


def resolve_model(model: Optional[str] = None) -> str:
    return model or os.environ.get("BEDAHREVIEW_MODEL", DEFAULT_MODEL)


def build_system_prompt() -> str:
    """The prompt file plus the output schema, so the model sees the exact theme values."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8").strip()
    return (
        PROMPT_PATH.read_text(encoding="utf-8")
        + "\n\n## output_schema.json\n\nEvery object you return must validate against this JSON Schema. "
        "Use only the exact `theme` values in its enum.\n\n```json\n" + schema + "\n```\n"
    )


def _generate(client: Any, model: str, system: str, messages: List[Dict[str, str]]) -> str:
    """One Gemini call. `messages` use roles user/assistant; Gemini calls the latter "model"."""
    from google.genai import types

    contents = [
        types.Content(role="model" if m["role"] == "assistant" else "user", parts=[types.Part(text=m["content"])])
        for m in messages
    ]
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        max_output_tokens=MAX_TOKENS,
        temperature=0,
    )
    response = client.models.generate_content(model=model, contents=contents, config=config)

    candidates = response.candidates or []
    if not candidates:
        raise ClassificationError("Gemini returned no candidates (prompt blocked?): %s" % (response.prompt_feedback,))
    finish = getattr(candidates[0].finish_reason, "name", str(candidates[0].finish_reason))
    if finish == "MAX_TOKENS":
        raise ClassificationError("reply was cut off at max_output_tokens; try a smaller batch")
    if finish not in ("STOP", "FINISH_REASON_UNSPECIFIED", "None"):
        raise ClassificationError("Gemini stopped with finish_reason=%s" % finish)
    parts = candidates[0].content.parts if candidates[0].content else []
    return "".join(
        part.text for part in parts or [] if getattr(part, "text", None) and not getattr(part, "thought", False)
    )


def _run_with_deadline(fn: Any, seconds: float) -> Any:
    """Run fn in a daemon thread and give up after `seconds`, since a hung socket can block for many minutes."""
    box: Dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError("no response from Gemini after %ds" % seconds)
    if "error" in box:
        raise box["error"]
    return box["value"]


def _is_transient(exc: BaseException) -> bool:
    """Worth retrying: a hang, a dropped connection, or an overloaded / rate-limited model. Not a bad key or bad request."""
    import httpx  # a dependency of google-genai, which raises its errors on dropped connections

    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    return getattr(exc, "code", None) in (429, 500, 502, 503, 504)


def _call(client: Any, model: str, system: str, messages: List[Dict[str, str]]) -> str:
    """_generate with a hard deadline, retrying temporary errors (overloaded model, rate limit, hang)."""
    for attempt, delay in enumerate((0,) + tuple(RETRY_DELAYS_S)):
        if delay:
            time.sleep(delay)
        try:
            return _run_with_deadline(lambda: _generate(client, model, system, messages), REQUEST_DEADLINE_S)
        except Exception as exc:
            if not _is_transient(exc) or attempt == len(RETRY_DELAYS_S):
                raise
    raise AssertionError("unreachable")


def _classify_batch(
    client: Any, model: str, system: str, reviews: List[Dict[str, Any]], product: Optional[str] = None
) -> List[Dict[str, Any]]:
    content = json.dumps(reviews, ensure_ascii=False)
    if product:
        content = "Product listing title: %s\nReviews:\n%s" % (product, content)
    messages = [{"role": "user", "content": content}]
    problems: List[str] = []
    results: List[Any] = []
    for attempt in range(2):  # one retry, telling the model what was wrong
        raw = _call(client, model, system, messages)
        try:
            results = _extract_json_array(raw)
            problems = validate_results(reviews, results)
        except ValueError as exc:  # includes json.JSONDecodeError
            results, problems = [], ["reply was not valid JSON: %s" % exc]
        if not problems:
            return results
        messages += [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "Your reply had these problems:\n- "
                + "\n- ".join(problems[:20])
                + "\nReturn the corrected full JSON array only.",
            },
        ]
    if results and all(p.endswith(_BAD_QUOTE) for p in problems):
        # Only unverifiable quotes are left: drop those reviews rather than show an invented quote.
        bad = {p.split()[1].rstrip(":") for p in problems}
        return [r for r in results if r["id"] not in bad]
    raise ClassificationError("invalid reply after retry:\n- " + "\n- ".join(problems[:20]))


def classify(
    reviews: List[Dict[str, Any]],
    client: Optional[Any] = None,
    model: Optional[str] = None,
    product: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Triage reviews. Each review: {"id", "stars", "variant", "date", "text"}.

    `product` is the listing title when known (for example read from a product link); it helps spot reviews about the wrong product.

    Returns one result per review, in input order, matching prompts/output_schema.json. Reviews with no text are
    answered in code (Ignore, "No text"). A review whose proof_quote still is not an exact substring after one retry
    is left out of the list instead of failing the batch.
    The API key is read from GEMINI_API_KEY by the Gemini client; it is never stored here.
    """
    if not reviews:
        return []
    ids = [str(r["id"]) for r in reviews]
    if len(set(ids)) != len(ids):
        raise ValueError("review ids must be unique")
    reviews = [dict(r, id=str(r["id"])) for r in reviews]

    client = client or make_client()
    model = resolve_model(model)
    system = build_system_prompt()

    # Star-only reviews go to Ignore without a model call.
    blank = {r["id"] for r in reviews if not (r.get("text") or "").strip()}
    texted = [r for r in reviews if r["id"] not in blank]
    by_id: Dict[str, Dict[str, Any]] = {r["id"]: no_text_result(r) for r in reviews if r["id"] in blank}
    for i in range(0, len(texted), BATCH_SIZE):
        for result in _classify_batch(client, model, system, texted[i : i + BATCH_SIZE], product):
            by_id[result["id"]] = result
    # Input order; a review whose quote could not be verified is missing from the list.
    return [by_id[r["id"]] for r in reviews if r["id"] in by_id]
