"""Build Part B's review batches for Homework 4 (homework/module-2/hw4.md).

Part B asks for four distinct batches, no trace counted twice:

    1. 15 uniformly sampled + 15 cluster representatives   (this script, "batch1")
    2. 30 traces stratified by one product dimension        (later)
    3. 25 traces from depth searches on candidate modes      (later)
    4. 15 more uniformly sampled, at the end, to check for new modes (later)

This script only does the mechanical sampling -- deciding *which* traces
enter the review set. The actual open coding (reading each one and writing a
first-failure note, or "no failure observed") is done by a human in
analysis/review_app, not here.

Reuses two pieces already in the repo rather than reinventing them:

  - analysis.helpers.selection: the deterministic k-means clustering and
    uniform-random picking Module 2's error-analysis skill already ships.
  - analysis.review_app.server._split_into_runs: the same duplicate-run
    detection the review interface uses, so a conversation key produced here
    (e.g. "support-0243::run2") matches what /api/conversations actually
    serves. Sampling with a different grouping than the interface renders
    would silently produce "no conversation found" picks.

Run it:

    uv run python analysis/build_sample_batches.py batch1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST_PATH = ROOT / "analysis" / "state" / "sample_manifest.json"

SEED = 7  # analysis.helpers.selection's own default; kept for reproducibility.


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


def _conversation_pool() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """One representative (first-turn) trace per conversation, plus a map
    from that representative's trace_id to the conversation key review_app
    would use (matching its scenario_id / scenario_id::runN scheme)."""
    from analysis.helpers import langfuse_io
    from analysis.review_app.server import _split_into_runs, _SESSION_KEY

    print("fetching traces from Langfuse (one API call per trace)...")
    raw = langfuse_io.fetch_traces(limit=1000)
    print(f"fetched {len(raw)} traces")

    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in raw:
        sid = t.get("meta", {}).get(_SESSION_KEY)
        if sid:
            by_scenario[sid].append(t)

    # 87 of this project's 450 traces have output: null on every observation,
    # including the model's own generation span -- confirmed via the raw API,
    # not a rendering gap. Root cause (per the student): the model provider's
    # billing credits ran out mid-run while generating HW3's scenario traces,
    # so those 87 scenario executions produced no captured output; the
    # scenario runner then re-ran them successfully once credits were
    # restored, ~7-20 minutes later. Every one of the 87 has such a working
    # sibling under the same scenario_id (verified: 0 unrecoverable cases).
    # _split_into_runs already keeps these as separate conversations, by
    # design (they are independent execution attempts of the same scenario,
    # not sequential turns of one conversation) -- and the review interface
    # is right to show both. But for SAMPLING specifically, always picking
    # run_index 0 (whichever sorts first chronologically -- always the
    # broken attempt here, since the failed run always precedes its retry)
    # would waste a review slot on a trace with nothing to review and
    # features (tool_call_count etc.) of 0 that don't reflect what the
    # scenario actually does. So: within one scenario's runs, prefer a run
    # with real output as that scenario's representative for clustering and
    # sampling, falling back to run_index 0 only if every run is empty.
    def _run_has_output(run: list[dict[str, Any]]) -> bool:
        return any(t.get("output") is not None for t in run)

    pool: list[dict[str, Any]] = []
    key_by_trace_id: dict[str, str] = {}
    for sid, group in by_scenario.items():
        runs = _split_into_runs(group)
        preferred_index = next(
            (i for i, run in enumerate(runs) if _run_has_output(run)), 0
        )
        for run_index, run in enumerate(runs):
            key = sid if run_index == 0 else f"{sid}::run{run_index + 1}"
            if run_index == preferred_index:
                rep = run[0]
                pool.append(rep)
                key_by_trace_id[rep["id"]] = key
            # A non-preferred empty run is still a valid, viewable
            # conversation in the interface -- it just isn't offered to the
            # sampler as a pick, since it carries no reviewable content.
    print(f"grouped into {len(pool)} distinct conversations "
          f"(preferring a run with real output per scenario when one exists)")
    return pool, key_by_trace_id


def _read_manifest() -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _write_manifest(entries: list[dict[str, Any]]) -> None:
    MANIFEST_PATH.write_text(json.dumps(entries, indent=2) + "\n")


def build_batch1() -> None:
    from analysis.helpers import selection

    pool, key_by_trace_id = _conversation_pool()

    existing = _read_manifest()
    already_sampled = {e["scenario_id"] for e in existing}
    # exclude conversations already in the manifest from a prior run of this
    # script, so re-running it is additive rather than duplicating picks.
    eligible_pool = [t for t in pool if key_by_trace_id[t["id"]] not in already_sampled]

    uniform = selection.select(eligible_pool, k=15, strategy="random", exclude_ids=set(), seed=SEED)
    uniform_ids = {p["trace_id"] for p in uniform}

    # Ask for k=23 under the "diversity" strategy: it splits 2/3 cluster
    # representatives + 1/3 random internally (n_rep = k*2//3), and k=23 is
    # the smallest k that yields exactly 15 representatives. Keep only the
    # "cluster * representative" picks and drop its random third -- batch 1's
    # uniform half is already covered by the call above, with no overlap
    # (uniform_ids is excluded from this pool).
    remaining_pool = [t for t in eligible_pool if t["id"] not in uniform_ids]
    diversity = selection.select(remaining_pool, k=23, strategy="diversity", exclude_ids=set(), seed=SEED)
    cluster_reps = [p for p in diversity if p["reason"].startswith("cluster")][:15]

    if len(cluster_reps) < 15:
        print(f"WARNING: only found {len(cluster_reps)} cluster representatives "
              f"(pool may be too small or too homogeneous); continuing with what's available.")

    new_entries = [
        {"scenario_id": key_by_trace_id[p["trace_id"]], "reason": p["reason"], "batch": "batch1_uniform"}
        for p in uniform
    ] + [
        {"scenario_id": key_by_trace_id[p["trace_id"]], "reason": p["reason"], "batch": "batch1_cluster"}
        for p in cluster_reps
    ]

    _write_manifest(existing + new_entries)
    print(f"added {len(new_entries)} conversations to {MANIFEST_PATH}")
    print(f"  batch1_uniform: {len(uniform)}")
    print(f"  batch1_cluster: {len(cluster_reps)}")
    print(f"  manifest total: {len(existing) + len(new_entries)}")


def _scenario_intents() -> dict[str, str]:
    """Map scenario_id -> tuple.intent from the scenario *definitions*
    (not the run results -- intent isn't captured in the Langfuse trace data
    at all, only in scenarios/*_scenarios.jsonl's tuple)."""
    out: dict[str, str] = {}
    for name in ("support_scenarios.jsonl", "pilot_scenarios.jsonl"):
        path = ROOT / "scenarios" / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            sid = rec.get("id")
            intent = rec.get("tuple", {}).get("intent")
            if sid and intent:
                out[sid] = intent
    return out


def _base_scenario_id(key: str) -> str:
    """Strip a "::runN" suffix (see _split_into_runs) to recover the id
    scenario definitions and intent are keyed by."""
    return key.split("::run")[0]


def build_batch2() -> None:
    from analysis.helpers import selection

    pool, key_by_trace_id = _conversation_pool()
    intents = _scenario_intents()

    existing = _read_manifest()
    already_sampled = {e["scenario_id"] for e in existing}
    eligible_pool = [t for t in pool if key_by_trace_id[t["id"]] not in already_sampled]

    by_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in eligible_pool:
        key = key_by_trace_id[t["id"]]
        intent = intents.get(_base_scenario_id(key))
        if intent:
            by_intent[intent].append(t)

    values = sorted(by_intent)
    if not values:
        print("no traces with a known intent found; nothing to sample")
        return

    # Distribute 30 evenly across however many intent values exist, not
    # proportionally to their natural frequency -- proportional would just be
    # a fancier random sample. The remainder (30 % len(values)) goes to the
    # alphabetically first values, so the split is deterministic.
    target_total = 30
    base_count, remainder = divmod(target_total, len(values))
    counts = {v: base_count + (1 if i < remainder else 0) for i, v in enumerate(values)}

    picked: list[dict[str, str]] = []
    running_exclude: set[str] = set()
    for value in values:
        bucket = by_intent[value]
        k = counts[value]
        picks = selection.select(bucket, k=k, strategy="random", exclude_ids=running_exclude, seed=SEED)
        if len(picks) < k:
            print(f"WARNING: intent={value} only has {len(picks)} available "
                  f"(wanted {k}); pool may be exhausted for this value.")
        for p in picks:
            running_exclude.add(p["trace_id"])
            picked.append({
                "scenario_id": key_by_trace_id[p["trace_id"]],
                "reason": f"stratified by intent={value}",
                "batch": "batch2_intent",
            })

    _write_manifest(existing + picked)
    print(f"added {len(picked)} conversations to {MANIFEST_PATH}")
    for value in values:
        print(f"  intent={value}: target {counts[value]}, "
              f"picked {sum(1 for p in picked if p['reason'] == f'stratified by intent={value}')}")
    print(f"  manifest total: {len(existing) + len(picked)}")


def build_batch4() -> None:
    """15 additional uniformly sampled traces, reviewed after the taxonomy
    has stabilized (batches 1-3), to check whether new modes keep appearing.
    Plain uniform sampling -- no stratification, no depth search -- since the
    point here is an unbiased check, not more targeted evidence."""
    from analysis.helpers import selection

    pool, key_by_trace_id = _conversation_pool()

    existing = _read_manifest()
    already_sampled = {e["scenario_id"] for e in existing}
    eligible_pool = [t for t in pool if key_by_trace_id[t["id"]] not in already_sampled]

    picks = selection.select(eligible_pool, k=15, strategy="random", exclude_ids=set(), seed=SEED)
    new_entries = [
        {"scenario_id": key_by_trace_id[p["trace_id"]], "reason": p["reason"], "batch": "batch4_final_check"}
        for p in picks
    ]

    _write_manifest(existing + new_entries)
    print(f"added {len(new_entries)} conversations to {MANIFEST_PATH}")
    print(f"  manifest total: {len(existing) + len(new_entries)}")


BATCHES = {"batch1": build_batch1, "batch2": build_batch2, "batch4": build_batch4}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", choices=sorted(BATCHES))
    args = parser.parse_args()
    BATCHES[args.batch]()


if __name__ == "__main__":
    main()
