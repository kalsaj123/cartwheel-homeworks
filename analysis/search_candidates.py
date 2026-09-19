"""Part B batch 3 / Part D depth search: retrieve candidate instances of
existing modes and close negatives, via semantic-neighbor similarity to
already-confirmed positives.

This only *retrieves candidates* -- it writes them to
analysis/state/suggestions.json as pending items in the review interface's
existing AI-suggestion queue (Progress tab). A human reviews and explicitly
accepts or dismisses each one; nothing here writes an annotation or a label.
Per the handout: "Treat a similarity score, model prediction, or
deterministic filter as a retrieval signal rather than a label. Review every
returned trace yourself. Record whether you accepted or rejected each
suggestion."

Also appends the same scenario_ids to analysis/state/sample_manifest.json
under batch "batch3_depth", since this batch does double duty: building the
100-trace review set (Part B) and validating the taxonomy (Part D).

Run it:

    uv run python analysis/search_candidates.py
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
SEED = 7

# Each target: a mode name (matches patterns.json where the mode is already
# confirmed; a working label for the two watch items, which aren't in
# patterns.json yet), seed trace_ids (its current confirmed positives), and
# how many candidates to pull. Allocation rationale is in the chat: priority
# to overstates_refund_capability (2/3 positives, needs a third most), then
# close negatives for the three well-supported modes, then the two thin
# single-instance watch items, then a broader RESP-1 consistency check.
CLOSE_NEGATIVE_ROUND = [
    {"mode": "gratuitous_escalation_offer",
     "seeds": ["0d0330ac11e331f759b13d7eb98bb6e1", "e342672c0aa0af57fb6fe322f025255a",
               "9da2661fa9fadea67b4c90939580cd22", "8fc7ee3da7ca06196656b6ab51b6f01c"],
     "k": 5},
    {"mode": "unnecessary_reconfirmation",
     "seeds": ["fd13946add1b702fb06446478a461dbd", "69567607a899d6e631dc68e24c4d93ce"],
     "k": 5},
    {"mode": "data_quality_issue_handling",
     "seeds": ["207549a7410504a6d66e8eb713813bc6", "19dfcb2677f6a06ad1f82b449889927c",
               "e4d2ddb597d9a1be6bee38873396a04a"],
     "k": 5},
    {"mode": "overstates_refund_capability",
     "seeds": ["937293c57f71b50f0317836b74752230", "3cff4d4c13dac4a454a5cfb804f1c66d"],
     "k": 3},
    {"mode": "dispute_misidentified_as_refund",
     "seeds": ["90900fc30666311899039a2a77de99a5", "f4c594937e7c1011962966db56cbfbbc"],
     "k": 3},
]

# Part D round 2 (2026-09-18): searching for CLOSE NEGATIVES on the 5
# LLM-judge modes with the thinnest negative counts (1-2 each), not more
# positives -- same "enrich" mechanism (semantic neighbor of confirmed
# positives), but the hoped-for outcome this time is "this reads similar but
# is handled correctly," not "this is another failure." Still a retrieval
# signal, not a label -- every result gets reviewed and accepted/rejected the
# same as any other suggestion.
TARGETS = CLOSE_NEGATIVE_ROUND
BATCH_TAG = "partD_close_negative_search"


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


def _read_manifest() -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


# Per-mode keywords: prefer a line that actually contains the evidence a
# reviewer needs to see, rather than the reply's generic opening line (e.g.
# "Here's what I found:" tells a reviewer nothing about *why* this trace was
# suggested for overstates_refund_capability).
_MODE_KEYWORDS = {
    "overstates_refund_capability": ["process the refund", "issue the refund", "i can process", "i can issue"],
    "gratuitous_escalation_offer": ["escalate"],
    "store_override_handling": ["store", "override", "platform default"],
    "unnecessary_reconfirmation": ["go ahead", "want me to", "would you like me to"],
    "dispute_misidentified_as_refund": ["refund", "dispute"],
    "data_quality_issue_handling": ["escalate", "ticket", "reach out", "flagged"],
    "policy_id_shopper_exposure": ["cw-"],
}


def _short_quote(text: str, mode: str, max_len: int = 150) -> str:
    """A line containing mode-relevant evidence when one exists, else the
    first non-empty line. Single line only -- keeps the quote free of
    newlines so the browser's exact-substring highlight match
    (el.textContent.indexOf) doesn't break on <br>-collapsed whitespace."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return text[:max_len]
    keywords = _MODE_KEYWORDS.get(mode, [])
    for line in lines:
        lowered = line.lower()
        if any(kw in lowered for kw in keywords):
            return line[:max_len]
    return lines[0][:max_len]


def main() -> None:
    from analysis.helpers import langfuse_io, selection
    from analysis.review_app.server import _live_conversations

    print("fetching all traces from Langfuse...")
    all_traces = langfuse_io.fetch_traces(limit=1000)
    print(f"fetched {len(all_traces)} traces")

    print("building conversations (same grouping as the review interface)...")
    conversations = _live_conversations(limit=1000)

    # trace_id -> (conversation key, flattened item list with idx positions)
    trace_location: dict[str, tuple[str, list[dict[str, Any]], int]] = {}
    for key, conv in conversations.items():
        flat: list[dict[str, Any]] = []
        for ti, turn in enumerate(conv["turns"]):
            if ti > 0:
                flat.append({"role": "turn_boundary"})
            for m in turn["messages"]:
                flat.append(m)
                if m.get("role") == "assistant" and m.get("text"):
                    # overwritten on each match, so this ends up as the LAST
                    # assistant message in the turn (consistent with the UI's
                    # own finalReplyForTurn())
                    trace_location[turn["trace_id"]] = (key, flat, len(flat) - 1)

    existing = _read_manifest()
    already_sampled_keys = {e["scenario_id"] for e in existing}
    already_labeled: set[str] = set()
    for key in already_sampled_keys:
        conv = conversations.get(key)
        if conv:
            already_labeled.update(t["trace_id"] for t in conv["turns"])

    new_manifest_entries: list[dict[str, Any]] = []
    new_suggestions: list[dict[str, Any]] = []
    seen_this_run: set[str] = set()

    for target in TARGETS:
        mode = target["mode"]
        candidates = selection.next_candidates(
            all_traces, mode=mode, k=target["k"], strategy="enrich",
            confirmed_failures=target["seeds"],
            already_labeled=already_labeled | seen_this_run,
            seed=SEED,
        )
        print(f"{mode}: requested {target['k']}, got {len(candidates)}")
        for c in candidates:
            trace_id = c["trace_id"]
            loc = trace_location.get(trace_id)
            if loc is None:
                print(f"  WARNING: {trace_id[:12]}... has no assistant reply to quote; skipping")
                continue
            key, flat, idx = loc
            quote = _short_quote(flat[idx].get("text", ""), mode)
            seen_this_run.add(trace_id)
            if key not in already_sampled_keys:
                new_manifest_entries.append({
                    "scenario_id": key,
                    "reason": f"depth search for {mode} ({c['signal']})",
                    "batch": BATCH_TAG,
                })
                already_sampled_keys.add(key)
            new_suggestions.append({
                "id": f"sugg-{mode}-{trace_id[:8]}-{int(time.time() * 1000) % 100000}",
                "trace_id": trace_id,
                "mode": mode,
                "quote": quote,
                "idx": idx,
            })

    MANIFEST_PATH.write_text(json.dumps(existing + new_manifest_entries, indent=2) + "\n")
    existing_suggestions = json.loads(SUGGESTIONS_PATH.read_text()) if SUGGESTIONS_PATH.exists() else []
    if not isinstance(existing_suggestions, list):
        existing_suggestions = []
    SUGGESTIONS_PATH.write_text(json.dumps(existing_suggestions + new_suggestions, indent=2) + "\n")

    print(f"\nadded {len(new_manifest_entries)} new conversations to sample_manifest.json "
          f"(manifest total: {len(existing) + len(new_manifest_entries)})")
    print(f"wrote {len(new_suggestions)} suggestions to suggestions.json for review "
          f"(existing: {len(existing_suggestions)}, total: {len(existing_suggestions) + len(new_suggestions)})")


if __name__ == "__main__":
    main()
