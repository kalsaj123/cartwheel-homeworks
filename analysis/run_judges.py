"""HW5 judge pipeline for the unnecessary_reconfirmation failure mode.

Run from the repository root, e.g.:

    uv run python -c "from analysis.run_judges import prepare_inputs; prepare_inputs()"
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
MODE = "unnecessary_reconfirmation"
STATE_DIR = REPO_ROOT / "analysis" / "state"
LABELS_PATH = STATE_DIR / "labels" / f"{MODE}.jsonl"
TRACE_INPUTS_PATH = STATE_DIR / "hw5_trace_inputs.json"


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from .env into the environment (same pattern as
    analysis/review_app/server.py and the other analysis/ scripts) so a judge
    run works without first running `source .env` by hand."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(REPO_ROOT / ".env")


def _load_labels() -> list[dict[str, Any]]:
    """Current label per trace_id (last row wins; label files are append-only)."""
    live: dict[str, dict[str, Any]] = {}
    with LABELS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            live[row["trace_id"]] = row
    return list(live.values())


def _find_turn(
    conversations: dict[str, dict[str, Any]], trace_id: str
) -> tuple[str | None, int | None]:
    """Locate (conversation_key, turn_index) for a labeled trace_id."""
    for key, conv in conversations.items():
        for i, turn in enumerate(conv["turns"]):
            if turn["trace_id"] == trace_id:
                return key, i
    return None, None


def prepare_inputs() -> list[dict[str, Any]]:
    """Build judge-ready trace inputs for every unnecessary_reconfirmation label.

    Reuses the review interface's own conversation grouping (same scenario_id,
    split into separate runs on a time gap) so a labeled trace_id resolves to
    exactly the turn(s) shown when it was labeled -- not a naive re-merge that
    could fold an unrelated retry back in.

    Each output record has only what the judge needs to decide: the request,
    any earlier turns, and the tool calls/results/policy passages leading to
    the final reply. Human labels, notes, and scenario `expected` metadata are
    never included.

    Writes analysis/state/hw5_trace_inputs.json and returns the records.
    """
    from analysis.review_app.server import _get_conversations

    conversations, source = _get_conversations(limit=1000, refresh=True)
    print(f"[prepare_inputs] {len(conversations)} conversations from {source}")

    labels = _load_labels()
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for label in labels:
        trace_id = label["trace_id"]
        key, turn_idx = _find_turn(conversations, trace_id)
        if key is None:
            missing.append(trace_id)
            continue
        turns = conversations[key]["turns"][: turn_idx + 1]
        messages: list[dict[str, Any]] = []
        for turn in turns:
            messages.extend(turn["messages"])
        records.append({"trace_id": trace_id, "trace": messages})

    if missing:
        raise ValueError(
            f"{len(missing)} labeled trace ids were not found in the fetched "
            f"conversations (try a higher limit or check Langfuse is reachable): "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
        )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_INPUTS_PATH.write_text(json.dumps(records, indent=2))
    print(f"[prepare_inputs] wrote {len(records)} records -> {TRACE_INPUTS_PATH}")
    return records


def split_data(mode: str) -> dict[str, list[str]]:
    """Split human labels for ``mode`` into train/dev/test (20/40/40, seed 7).

    Run once. Re-running reshuffles which traces are held out, defeating the
    point of a test set held back until the judge is frozen. Restricted to
    the trace ids actually present in hw5_trace_inputs.json, so a label
    without a matching prepared input can't sneak into a split.
    """
    from analysis.helpers import split_labels

    records = json.loads(TRACE_INPUTS_PATH.read_text())
    splits = split_labels(
        mode,
        fractions=(0.20, 0.40, 0.40),
        seed=7,
        min_per_class=10,
        eligible_trace_ids=[record["trace_id"] for record in records],
    )
    for name in ("train", "dev", "test"):
        labels_by_id = {row["trace_id"]: row["label"] for row in _load_labels()}
        n_fail = sum(labels_by_id[tid] == 1 for tid in splits[name])
        n_pass = sum(labels_by_id[tid] == 0 for tid in splits[name])
        print(f"[split_data] {name}: {len(splits[name])} traces ({n_fail} fail / {n_pass} pass)")
    return splits


def run_development(mode: str, prompt_path: str) -> dict[str, Any]:
    """Register a prompt version, run it on the dev split, and score it.

    A paid step: dispatches one live model call per dev trace through
    DocETL. Call this only after the caller has confirmed the model and
    trace count.
    """
    from analysis.helpers import judge_alignment, register_judge, run_judge

    record = register_judge(
        mode=mode,
        prompt_text=Path(prompt_path).read_text(),
        judge_model="gpt-4o-mini",
    )
    judge_id = record["judge_id"]
    run_judge(judge_id, split="dev", batch_size=10)
    development = judge_alignment(judge_id, split="dev")

    report_dir = REPO_ROOT / "analysis" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"dev-{judge_id}.json"
    report_path.write_text(json.dumps(development, indent=2))
    print(f"[run_development] judge_id={judge_id}")
    print(f"[run_development] TPR={development['tpr']:.2f} {development['tpr_interval']}  "
          f"TNR={development['tnr']:.2f} {development['tnr_interval']}")
    print(f"[run_development] wrote {report_path}")
    return {"judge_id": judge_id, "development": development}


def run_test(judge_id: str) -> dict[str, Any]:
    """Freeze ``judge_id``, evaluate it once on the held-out test split, and
    save the metrics. Freezing is one-way; call this only after development
    iteration is done, and never call it twice for the same judge_id."""
    from analysis.helpers import freeze_judge, judge_alignment, run_judge

    freeze_judge(judge_id)
    run_judge(judge_id, split="test", batch_size=10)
    test = judge_alignment(judge_id, split="test")

    report_dir = REPO_ROOT / "analysis" / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"test-{judge_id}.json"
    report_path.write_text(json.dumps(test, indent=2))
    print(f"[run_test] judge_id={judge_id} (frozen)")
    print(f"[run_test] TPR={test['tpr']:.2f} {test['tpr_interval']}  "
          f"TNR={test['tnr']:.2f} {test['tnr_interval']}")
    print(f"[run_test] wrote {report_path}")
    return {"judge_id": judge_id, "test": test}
