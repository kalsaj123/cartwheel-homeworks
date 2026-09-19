"""Part E: apply the 2 code-check modes' structural verdicts across every
trace in the 125-trace review set.

Different job from search_structural.py: that script searches the full
corpus for NEW candidate traces to add to the sample. This script assigns a
present/absent verdict to traces ALREADY in analysis/state/sample_manifest.json,
for the two modes classified evaluator_type: "code check" in patterns.json
(store_override_handling, asserts_eligibility_from_missing_date).

This is a suggestion, not a label. It writes to
analysis/state/structural_suggestions.json for the review interface's
Labeling grid to show as a pre-filled, one-click-to-confirm cell -- the
student still confirms (or overrides) every cell themselves. A trace/mode
pair with an existing confirmed label (analysis/state/labels/<mode>.jsonl) is
left alone. A pair the script can't determine structurally (see
store_override_handling's symptom-2 case below) is left with no suggestion
at all, not a guessed one.

Run it:

    uv run python analysis/apply_structural_checks.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST_PATH = ROOT / "analysis" / "state" / "sample_manifest.json"
PATTERNS_PATH = ROOT / "analysis" / "state" / "patterns.json"
LABELS_DIR = ROOT / "analysis" / "state" / "labels"
OUT_PATH = ROOT / "analysis" / "state" / "structural_suggestions.json"

MODES = ("asserts_eligibility_from_missing_date", "store_override_handling")

_ELIGIBILITY_VERDICT_PHRASES = [
    "return window has passed", "no longer eligible", "not eligible",
    "outside the return window", "within the return window",
    "days from delivery", "days from the delivery", "day return", "return period",
    "return window is",
]
_MISSING_DATE_ACKNOWLEDGED_PHRASES = [
    "delivery date is missing", "don't have a delivery date",
    "delivery date isn't recorded", "no delivery date on file",
    "missing the delivery date",
]
_FEE_PHRASES = ["restocking fee", "% fee", "fee of", "fee applies"]

_STORE_ID_TO_POLICY = {
    2: "store-juniper-home-goods-policy",
    5: "store-cascade-audio-policy",
    7: "store-northwind-books-policy",
    10: "store-meridian-cycles-policy",
    13: "store-saltbox-pantry-policy",
    15: "store-second-stitch-apparel-policy",
}

# Field-specific: per seed/generate.py's CORE_STORES table, no store in this
# seed overrides BOTH the return window and the restocking fee. A window
# question at a fee-only-override store (or vice versa) is correctly
# answered from the platform default with no store-specific fetch needed --
# checked directly against the seed generator, not assumed. (This distinction
# is what the first version of this check missed, producing 2 of its first 4
# false positives against Part D's already-confirmed close negatives.)
_WINDOW_OVERRIDE_STORES = {2, 7, 10, 13}  # Juniper 14d, Northwind 45d, Meridian 21d, Saltbox 7d
_FEE_OVERRIDE_STORES = {5, 15}  # Cascade Audio, Second Stitch Apparel


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


def _existing_labels(mode: str) -> set[str]:
    path = LABELS_DIR / f"{mode}.jsonl"
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("trace_id"):
            out.add(rec["trace_id"])
    return out


def _caller_store_id(trace: dict[str, Any]) -> int | None:
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


def _all_assistant_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(m.get("text", "") for m in messages if m.get("role") == "assistant" and m.get("text"))


def check_asserts_eligibility(trace: dict[str, Any]) -> tuple[int | None, str]:
    messages = trace.get("trace") or []
    order_result = None
    for m in messages:
        if m.get("role") == "tool_result" and m.get("name") == "get_order":
            content = m.get("content")
            if isinstance(content, dict) and content.get("ok", True) is not False:
                order_result = content.get("order", content)
    if not order_result:
        return 0, "no get_order result in this trace; trigger condition doesn't apply"
    if order_result.get("status") != "delivered":
        return 0, "trigger condition not met (order not delivered)"

    delivered_at = order_result.get("delivered_at")
    shipped_at = order_result.get("shipped_at")
    # Merged 2026-09-19: a required date can fail two ways -- entirely absent
    # (delivered_at is null), or present but chronologically impossible
    # (shipped_at recorded after delivered_at, seed/generate.py's
    # dq-order-reversed-dates). Both are "asserted a timeline without valid
    # dates to support it" -- same downstream failure, same fix.
    missing_date = delivered_at is None
    reversed_dates = delivered_at is not None and shipped_at is not None and shipped_at > delivered_at
    if not missing_date and not reversed_dates:
        return 0, "trigger condition not met (delivered_at present and chronologically consistent)"

    lowered = _all_assistant_text(messages).lower()
    asserts_verdict = any(p in lowered for p in _ELIGIBILITY_VERDICT_PHRASES)
    acknowledges_gap = any(p in lowered for p in _MISSING_DATE_ACKNOWLEDGED_PHRASES)
    defect = "delivered_at is null" if missing_date else f"shipped_at ({shipped_at}) is after delivered_at ({delivered_at})"
    if asserts_verdict and not acknowledges_gap:
        return 1, f"{defect} on a delivered order; reply asserts a verdict without acknowledging the date problem"
    if acknowledges_gap:
        return 0, f"{defect}; reply correctly flags the date problem instead of asserting a verdict"
    return 0, f"{defect}, but the reply makes no eligibility assertion either way"


def check_store_override(trace: dict[str, Any]) -> tuple[int | None, str]:
    messages = trace.get("trace") or []
    lowered = _all_assistant_text(messages).lower()
    asserts_window = any(p in lowered for p in _ELIGIBILITY_VERDICT_PHRASES)
    asserts_fee = any(p in lowered for p in _FEE_PHRASES)
    if not asserts_window and not asserts_fee:
        return 0, "no return-window or restocking-fee conclusion given; trigger condition doesn't apply"

    role = trace.get("meta", {}).get("role")
    store_id: int | None = None
    if role == "merchant":
        store_id = _caller_store_id(trace)
    else:
        for m in messages:
            if m.get("role") == "tool_result" and m.get("name") == "get_order":
                content = m.get("content")
                if isinstance(content, dict):
                    order = content.get("order", content)
                    if isinstance(order, dict) and order.get("store_id") is not None:
                        store_id = order["store_id"]

    if store_id is None:
        return None, "could not determine the relevant store from this trace's tool calls; needs a manual read"

    # Only the field actually asserted matters -- a window question at a
    # fee-only-override store (or vice versa) is correctly platform-default,
    # no store-specific fetch required.
    needs_override_check = (asserts_window and store_id in _WINDOW_OVERRIDE_STORES) or (
        asserts_fee and store_id in _FEE_OVERRIDE_STORES
    )
    if not needs_override_check:
        return 0, f"store {store_id} has no override for the field asserted here; the platform default is correct by definition"

    correct_policy_id = _STORE_ID_TO_POLICY[store_id]
    fetched_store_policy_ids = []
    for m in messages:
        if m.get("role") != "tool_result":
            continue
        content = m.get("content")
        if not isinstance(content, dict):
            continue
        if m.get("name") == "get_policy" and str(content.get("policy_id", "")).startswith("store-"):
            fetched_store_policy_ids.append(content["policy_id"])
        elif m.get("name") == "search_help_center":
            # search_help_center can surface a store policy doc directly in
            # its ranked results, grounding the reply without a separate
            # get_policy call -- missing this produced a false positive
            # (3cff4d4c13da..., a Cascade Audio restocking-fee mention
            # sourced entirely from a search result).
            for result in content.get("results") or []:
                if isinstance(result, dict) and str(result.get("policy_id", "")).startswith("store-"):
                    fetched_store_policy_ids.append(result["policy_id"])

    if not fetched_store_policy_ids:
        return 1, f"reply asserts a conclusion but never fetched store {store_id}'s override policy ({correct_policy_id}) despite one existing"
    if correct_policy_id not in fetched_store_policy_ids:
        return 1, f"fetched a different store's policy ({fetched_store_policy_ids}) instead of the caller's own ({correct_policy_id})"
    return None, f"correct store policy ({correct_policy_id}) was fetched -- confirming the reply actually concludes for the caller (vs. describing the two-tier rule abstractly) needs a read"


def main() -> None:
    from analysis.helpers import langfuse_io
    from analysis.review_app.server import _live_conversations

    print("fetching all traces from Langfuse...")
    all_traces = {t["id"]: t for t in langfuse_io.fetch_traces(limit=1000)}
    print(f"fetched {len(all_traces)} traces")

    print("building conversations (for scenario_id -> trace_id mapping)...")
    conversations = _live_conversations(limit=1000)

    manifest = _read_json(MANIFEST_PATH, [])
    patterns = _read_json(PATTERNS_PATH, {"modes": []})
    known: dict[str, dict[str, int]] = {}
    for m in patterns.get("modes", []):
        name = m["name"]
        known[name] = {}
        for tid in m.get("example_trace_ids", []):
            known[name][tid] = 1
        for tid in m.get("close_negative_trace_ids", []):
            known[name][tid] = 0

    checkers = {
        "asserts_eligibility_from_missing_date": check_asserts_eligibility,
        "store_override_handling": check_store_override,
    }

    suggestions: list[dict[str, Any]] = []
    counts = {mode: {"1": 0, "0": 0, "none": 0, "skipped_already_labeled": 0} for mode in MODES}
    disagreements: list[str] = []

    for entry in manifest:
        scenario_id = entry["scenario_id"]
        conv = conversations.get(scenario_id)
        if not conv:
            continue
        for turn in conv["turns"]:
            trace_id = turn["trace_id"]
            trace = all_traces.get(trace_id)
            if trace is None:
                continue
            for mode in MODES:
                already_labeled = _existing_labels(mode)
                if trace_id in already_labeled:
                    counts[mode]["skipped_already_labeled"] += 1
                    continue
                label, signal = checkers[mode](trace)
                if label is None:
                    counts[mode]["none"] += 1
                    continue
                counts[mode][str(label)] += 1
                known_label = known.get(mode, {}).get(trace_id)
                if known_label is not None and known_label != label:
                    disagreements.append(
                        f"{mode}: trace {trace_id[:12]}... was confirmed {'present' if known_label else 'absent'} "
                        f"during Part D, but the structural check says {'present' if label else 'absent'} ({signal})"
                    )
                suggestions.append({
                    "trace_id": trace_id,
                    "scenario_id": scenario_id,
                    "mode": mode,
                    "suggested_label": label,
                    "signal": signal,
                    "matches_part_d": known_label == label if known_label is not None else None,
                })

    OUT_PATH.write_text(json.dumps(suggestions, indent=2) + "\n")

    print(f"\nwrote {len(suggestions)} structural suggestions to {OUT_PATH.relative_to(ROOT)}")
    for mode in MODES:
        c = counts[mode]
        print(f"  {mode}: present={c['1']} absent={c['0']} needs_manual_read={c['none']} already_labeled={c['skipped_already_labeled']}")

    if disagreements:
        print(f"\n!!! {len(disagreements)} disagreement(s) with Part D's confirmed calls -- review these first:")
        for d in disagreements:
            print(f"  - {d}")
    else:
        print("\nno disagreements with Part D's already-confirmed positives/close-negatives.")


if __name__ == "__main__":
    main()
