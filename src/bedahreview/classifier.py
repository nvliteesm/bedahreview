"""classify(): send pasted reviews to Gemini with the triage prompt and return validated results."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "prompts" / "classifier.md"
SCHEMA_PATH = ROOT / "prompts" / "output_schema.json"

DEFAULT_MODEL = "gemini-3.8-flash"
BATCH_SIZE = 15  # reviews per API call; fake-review signals only work inside one batch
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
            problems.append("review %s: proof_quote is not an exact substring of the review text" % rid)
        if quote is None and text.strip():
            problems.append("review %s: proof_quote is null but the review has text" % rid)
        bucket = result["bucket"]
        if (bucket == "fight") != (result.get("fight") is not None):
            problems.append("review %s: 'fight' must be set exactly when bucket is fight" % rid)
        if (bucket == "fix") != bool(result.get("fix_suggestion_id")):
            problems.append("review %s: 'fix_suggestion_id' must be set exactly when bucket is fix" % rid)
    return problems


def _call(client: Any, model: str, system: str, messages: List[Dict[str, str]]) -> str:
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
    return "".join(part.text for part in parts or [] if getattr(part, "text", None))


def _classify_batch(client: Any, model: str, system: str, reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    messages = [{"role": "user", "content": json.dumps(reviews, ensure_ascii=False)}]
    problems: List[str] = []
    for attempt in range(2):  # one retry, telling the model what was wrong
        raw = _call(client, model, system, messages)
        try:
            results = _extract_json_array(raw)
            problems = validate_results(reviews, results)
        except ValueError as exc:  # includes json.JSONDecodeError
            problems = ["reply was not valid JSON: %s" % exc]
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
    raise ClassificationError("invalid reply after retry:\n- " + "\n- ".join(problems[:20]))


def classify(
    reviews: List[Dict[str, Any]],
    client: Optional[Any] = None,
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Triage reviews. Each review: {"id", "stars", "variant", "date", "text"}.

    Returns one result per review, in input order, matching prompts/output_schema.json.
    The API key is read from GEMINI_API_KEY by the Gemini client; it is never stored here.
    """
    if not reviews:
        return []
    ids = [str(r["id"]) for r in reviews]
    if len(set(ids)) != len(ids):
        raise ValueError("review ids must be unique")
    reviews = [dict(r, id=str(r["id"])) for r in reviews]

    if client is None:
        from google import genai

        client = genai.Client()  # reads GEMINI_API_KEY (or GOOGLE_API_KEY) from the environment
    model = model or os.environ.get("BEDAHREVIEW_MODEL", DEFAULT_MODEL)
    system = PROMPT_PATH.read_text(encoding="utf-8")

    results: List[Dict[str, Any]] = []
    for i in range(0, len(reviews), BATCH_SIZE):
        results += _classify_batch(client, model, system, reviews[i : i + BATCH_SIZE])
    return results
