"""Part D depth search for the two code-check modes, via exact structural
queries instead of semantic similarity.

Unlike search_candidates.py's "enrich" search (bag-of-words similarity to a
mode's confirmed positives -- the right tool for the LLM-judge modes, where
the failure is a phrasing pattern with no fixed lexical form), these two
modes were classified as code checks specifically because their failure
condition IS a structural fact: a null field, or a mismatched ID. So this
script queries for that structural fact directly, which is both more precise
and cheaper than similarity search for these two.

Writes candidates to the same suggestions.json queue as search_candidates.py
-- still a retrieval signal, not a label. Every result gets reviewed and
accepted/rejected by the student like any other suggestion.

Run it:

    uv run python analysis/search_structural.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST_PATH = ROOT / "analysis" / "state" / "sample_manifest.json"
SUGGESTIONS_PATH = ROOT / "analysis" / "state" / "suggestions.json"
BATCH_TAG = "partD_structural_search"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    import os
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def _short_quote(text: str, max_len: int = 150) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:max_len]
    return text[:max_len]


# A definitive eligibility verdict, in this product's actual (fairly
# templated) phrasing -- see interface_comparison.md and workshop_notes.md
# for examples this was drawn from.
_ELIGIBILITY_VERDICT_PHRASES = [
    "return window has passed", "no longer eligible", "not eligible",
    "outside the return window", "within the return window",
    "days from delivery", "day return", "return period",
]
_MISSING_DATE_ACKNOWLEDGED_PHRASES = [
    "delivery date is missing", "don't have a delivery date",
    "delivery date isn't recorded", "no delivery date on file",
    "missing the delivery date",
]

# store_id (int, from the stores table) -> the policy_id that actually
# belongs to it (from data/policies/store-*.md's own policy_id front matter).
# Only the six stores with a documented override/opt-in have their own page.
_STORE_ID_TO_POLICY = {
    2: "store-juniper-home-goods-policy",
    5: "store-cascade-audio-policy",
    7: "store-northwind-books-policy",
    10: "store-meridian-cycles-policy",
    13: "store-saltbox-pantry-policy",
    15: "store-second-stitch-apparel-policy",
}


def _caller_store_id(trace: dict[str, Any]) -> int | None:
    """cartwheel.store_id is set on every TOOL span for a merchant caller
    (observability/instrument.py's record_tool_result), regardless of
    whether get_order was ever called -- so it must be read from the raw
    observation metadata, not inferred from any one tool's result."""
    for obs in trace.get("observations") or []:
        meta = obs.get("metadata")
        if not isinstance(meta, dict):
            continue
        attrs = meta.get("attributes")
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                attrs = None
        if isinstance(attrs, dict) and attrs.get("cartwheel.store_id") is not None:
            try:
                return int(attrs["cartwheel.store_id"])
            except (TypeError, ValueError):
                continue
    return None


def find_missing_date_assertions(all_traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Traces where a get_order result has delivered_at: null (status
    delivered) and the final reply asserts a verdict without acknowledging
    the missing date."""
    hits = []
    for t in all_traces:
        messages = t.get("trace") or []
        order_result = None
        for m in messages:
            if m.get("role") == "tool_result" and m.get("name") == "get_order":
                content = m.get("content")
                if isinstance(content, dict):
                    order_result = content.get("order")
        if not order_result:
            continue
        if order_result.get("status") != "delivered" or order_result.get("delivered_at") is not None:
            continue
        reply = next((m.get("text", "") for m in reversed(messages) if m.get("role") == "assistant" and m.get("text")), "")
        lowered = reply.lower()
        asserts_verdict = any(p in lowered for p in _ELIGIBILITY_VERDICT_PHRASES)
        acknowledges_gap = any(p in lowered for p in _MISSING_DATE_ACKNOWLEDGED_PHRASES)
        if asserts_verdict and not acknowledges_gap:
            hits.append({"trace": t, "signal": "delivered_at null, reply asserts a verdict with no mention of the missing date", "kind": "positive"})
        elif acknowledges_gap:
            hits.append({"trace": t, "signal": "delivered_at null, reply correctly flags the missing date", "kind": "close_negative"})
    return hits


def find_store_misattribution(all_traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merchant-role traces where a get_policy call fetched a store policy
    document belonging to a DIFFERENT store than the caller's own."""
    hits = []
    for t in all_traces:
        if t.get("meta", {}).get("role") != "merchant":
            continue
        caller_store_id = _caller_store_id(t)
        if caller_store_id is None or caller_store_id not in _STORE_ID_TO_POLICY:
            continue  # only the six stores with their own page are checkable this way
        correct_policy_id = _STORE_ID_TO_POLICY[caller_store_id]

        fetched_store_policy_ids: list[str] = []
        for m in t.get("trace") or []:
            if m.get("role") == "tool_result" and m.get("name") == "get_policy":
                content = m.get("content")
                if isinstance(content, dict) and str(content.get("policy_id", "")).startswith("store-"):
                    fetched_store_policy_ids.append(content["policy_id"])

        for pid in fetched_store_policy_ids:
            if pid != correct_policy_id:
                hits.append({"trace": t, "signal": f"merchant's own store_id is {caller_store_id} ({correct_policy_id}), but reply used {pid}", "kind": "positive"})
            else:
                hits.append({"trace": t, "signal": f"merchant correctly used their own store's policy ({pid})", "kind": "close_negative"})
    return hits


def main() -> None:
    from analysis.helpers import langfuse_io
    from analysis.review_app.server import _live_conversations

    print("fetching all traces from Langfuse...")
    all_traces = langfuse_io.fetch_traces(limit=1000)
    print(f"fetched {len(all_traces)} traces")

    print("building conversations (for quote/idx lookup)...")
    conversations = _live_conversations(limit=1000)
    trace_location: dict[str, tuple[str, list[dict[str, Any]], int]] = {}
    for key, conv in conversations.items():
        flat: list[dict[str, Any]] = []
        for ti, turn in enumerate(conv["turns"]):
            if ti > 0:
                flat.append({"role": "turn_boundary"})
            for m in turn["messages"]:
                flat.append(m)
                if m.get("role") == "assistant" and m.get("text"):
                    trace_location[turn["trace_id"]] = (key, flat, len(flat) - 1)

    existing = _read_json(MANIFEST_PATH, [])
    already_sampled_keys = {e["scenario_id"] for e in existing}
    already_labeled_trace_ids: set[str] = set()
    for key in already_sampled_keys:
        conv = conversations.get(key)
        if conv:
            already_labeled_trace_ids.update(t["trace_id"] for t in conv["turns"])

    results = {
        "asserts_eligibility_from_missing_date": find_missing_date_assertions(all_traces),
        "store_policy_misattribution": find_store_misattribution(all_traces),
    }

    new_manifest_entries: list[dict[str, Any]] = []
    new_suggestions: list[dict[str, Any]] = []

    for mode, hits in results.items():
        kept = [h for h in hits if h["trace"]["id"] not in already_labeled_trace_ids]
        print(f"{mode}: {len(hits)} structural hits, {len(kept)} not already in the reviewed sample")
        for h in kept:
            trace_id = h["trace"]["id"]
            loc = trace_location.get(trace_id)
            if loc is None:
                continue
            key, flat, idx = loc
            quote = _short_quote(flat[idx].get("text", ""))
            if key not in already_sampled_keys:
                new_manifest_entries.append({
                    "scenario_id": key,
                    "reason": f"structural search for {mode} ({h['signal']})",
                    "batch": BATCH_TAG,
                })
                already_sampled_keys.add(key)
            new_suggestions.append({
                "id": f"sugg-struct-{mode}-{trace_id[:8]}-{int(time.time() * 1000) % 100000}",
                "trace_id": trace_id,
                "mode": mode,
                "quote": quote,
                "idx": idx,
                "structural_kind": h["kind"],  # "positive" or "close_negative" -- our guess, still needs human review
            })

    MANIFEST_PATH.write_text(json.dumps(existing + new_manifest_entries, indent=2) + "\n")
    existing_suggestions = _read_json(SUGGESTIONS_PATH, [])
    if not isinstance(existing_suggestions, list):
        existing_suggestions = []
    SUGGESTIONS_PATH.write_text(json.dumps(existing_suggestions + new_suggestions, indent=2) + "\n")

    print(f"\nadded {len(new_manifest_entries)} new conversations to sample_manifest.json")
    print(f"wrote {len(new_suggestions)} suggestions to suggestions.json for review")


if __name__ == "__main__":
    main()
