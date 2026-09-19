"""Review server for Homework 4 (built on top of the reference in
``analysis/server.py``; see ``analysis/report/interface_comparison.md`` for
what changed and why).

This is not the reference interface unchanged. The reference renders one
Langfuse trace (one user turn) at a time. Cartwheel creates one trace per
turn, so a multi-turn conversation is split across several traces, and the
reference has no notion of joining them back together. This server adds that
join (grouping by ``cartwheel.scenario_id``, the field the data actually
carries -- see the note on ``cartwheel.session_id`` below) plus the two views
the reference does not have at all: a structured present/absent labeling grid
and a taxonomy view.

Note on the join key. The handout says to group by ``cartwheel.session_id``.
Inspecting live traces (Part A, this file's sibling investigation) found that
field absent from every observation in every trace pulled from this project's
Langfuse instance, even though ``server/app.py`` sets it -- the recorded
traces evidently predate that line, or something upstream drops it. The only
join key actually present is ``cartwheel.scenario_id``, which is also what
``analysis/helpers/normalization.py``'s own ``_merge_multi_turn`` already
groups by. This server does the same. If ``cartwheel.session_id`` starts
showing up in fresher traces, switch ``_SESSION_KEY`` below.

API additions over the reference:

    GET  /api/conversations   traces grouped into full conversations
    GET  /api/labels          structured present/absent judgments
    POST /api/labels          save one judgment (writes the local .jsonl
                               under analysis/state/labels/ AND a Langfuse
                               score, mirroring how /api/annotations already
                               syncs accepted judgments)
    GET  /api/structural_suggestions   precomputed code-check verdicts
                               (analysis/apply_structural_checks.py) shown as
                               a pre-filled, one-click-to-confirm Labeling
                               grid cell -- never auto-written as a label

Run it:

    uv run python analysis/review_app/server.py            # serve on :8030
    uv run python analysis/review_app/server.py --port 8031
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
STATE_DIR = ROOT / "analysis" / "state"
LABELS_DIR = STATE_DIR / "labels"
UI_DIR = HERE / "ui"

sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines from ``.env`` into the environment.

    Without this, launching the server without first running
    ``source .env`` (easy to forget, and this repo has no python-dotenv
    dependency) silently falls back to the offline demonstration data --
    which has no tool calls at all, since ``scenarios/final-results.jsonl``
    only records the final user/agent text per turn. That looks like a
    rendering bug (missing tool calls) when it is really a missing-env bug,
    so load it unconditionally here rather than relying on the caller's
    shell. Existing environment variables always win (``setdefault``), so an
    explicit ``export`` still overrides the file.
    """
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


_load_dotenv(ROOT / ".env")

_SESSION_KEY = "scenario_id"  # see module docstring

# API path -> the state file it reads/writes. Same file-backed contract as
# the reference server, so the UI's poll loop and save flow are unchanged.
API_FILES: dict[str, Path] = {
    "/api/samples": STATE_DIR / "sample_manifest.json",
    "/api/annotations": STATE_DIR / "annotations.json",
    "/api/patterns": STATE_DIR / "patterns.json",
    "/api/suggestions": STATE_DIR / "suggestions.json",
}
API_DEFAULTS: dict[str, Any] = {
    "/api/samples": [],
    "/api/annotations": [],
    "/api/patterns": {},
    "/api/suggestions": [],
}


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Conversations: group per-turn traces into full sessions.
# ---------------------------------------------------------------------------

_conversation_cache: dict[str, dict[str, Any]] | None = None
_conversation_source: str = ""
_expectations_cache: dict[str, dict[str, Any]] | None = None


def _scenario_expectations() -> dict[str, dict[str, Any]]:
    """Map scenario_id -> {scenario_group, expected} from the committed HW3
    result files.

    This is ground truth about what a scenario was designed to test (and,
    critically, whether it carries a known data-quality issue -- see
    ``source.type == "data_quality_table"``), not something Langfuse's own
    trace data carries. Surfacing it in the conversation header gives a
    reviewer that context up front instead of only discovering a designed
    data-quality problem by re-deriving it from the raw order fields.
    """
    global _expectations_cache
    if _expectations_cache is not None:
        return _expectations_cache
    out: dict[str, dict[str, Any]] = {}
    for name in ("final-results.jsonl", "pilot-results.jsonl"):
        path = ROOT / "scenarios" / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            sid = rec.get("scenario_id")
            if sid and sid not in out:
                out[sid] = {
                    "scenario_group": rec.get("scenario_group"),
                    "expected": rec.get("expected"),
                }
    _expectations_cache = out
    return out


def _offline_conversations() -> dict[str, dict[str, Any]]:
    """Build conversations from the committed HW3 results (no Langfuse).

    Used only when Langfuse is not configured, per AGENTS.md: local files
    support an offline demonstration but do not replace Langfuse normally.
    """
    path = ROOT / "scenarios" / "final-results.jsonl"
    conversations: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return conversations
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        sid = rec.get("scenario_id")
        if not sid:
            continue
        turns = []
        for i, turn in enumerate(rec.get("turns") or []):
            turns.append(
                {
                    "trace_id": f"offline::{sid}::{i}",
                    "timestamp": None,
                    "messages": [
                        {"role": "user", "text": turn.get("user", "")},
                        {"role": "assistant", "text": turn.get("agent", "")},
                    ],
                    # Not derivable from final-results.jsonl (no tool-call
                    # detail is recorded there) -- left absent rather than
                    # guessed, so the UI can tell "unknown" from "zero".
                    "tool_call_count": None,
                }
            )
        conversations[sid] = {
            "scenario_id": sid,
            "meta": {
                "scenario_group": rec.get("scenario_group"),
                "model": rec.get("model"),
                "status": rec.get("status"),
                "expected": rec.get("expected"),
                "user_id": None,
            },
            "turns": turns,
        }
    return conversations


from datetime import datetime

# Two traces sharing a scenario_id are real sequential turns of one
# conversation only if they happened close together in time. Checked against
# this project's own data: a genuine followup turn lands ~10s after the
# opening turn (model latency only), while two independent scenario-runner
# executions of the *same* scenario (a pilot pass and a final pass, or a
# retry) land minutes apart and repeat the identical opening message. A wide
# margin between those two cases (10s vs 11-18min observed here) makes this
# threshold safe.
_MAX_TURN_GAP_SECONDS = 120


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _split_into_runs(group: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split same-scenario traces into separate conversations when the gap
    between consecutive traces is too large to be a real followup turn."""
    group = sorted(group, key=lambda t: t.get("timestamp") or "")
    runs: list[list[dict[str, Any]]] = []
    for t in group:
        ts = _parse_ts(t.get("timestamp"))
        if runs:
            prev_ts = _parse_ts(runs[-1][-1].get("timestamp"))
            if ts is not None and prev_ts is not None and ts - prev_ts <= _MAX_TURN_GAP_SECONDS:
                runs[-1].append(t)
                continue
        runs.append([t])
    return runs


_store_names_cache: dict[int, str] | None = None


def _store_names() -> dict[int, str]:
    """id -> name for every store, read straight from the seeded DB.

    Shown next to a merchant's store_id in the conversation header so a
    reviewer can check a reply's claimed store identity against ground truth
    without leaving the interface -- the gap that let a real misattribution
    (pilot-0015::run2: the agent said "Store ID 2 is Meridian Cycles"; it is
    actually Juniper Home Goods) go unnoticed during Part D's review.
    """
    global _store_names_cache
    if _store_names_cache is not None:
        return _store_names_cache
    import sqlite3

    db_path = ROOT / "data" / "cartwheel.db"
    names: dict[int, str] = {}
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            for row in conn.execute("SELECT id, name FROM stores"):
                names[row[0]] = row[1]
        finally:
            conn.close()
    _store_names_cache = names
    return names


def _live_conversations(limit: int) -> dict[str, dict[str, Any]]:
    from analysis.helpers import langfuse_io

    print(f"[conversations] fetching up to {limit} traces from Langfuse "
          "(one API call per trace; this can take a while)...")
    started = time.time()
    raw = langfuse_io.fetch_traces(limit=limit)
    print(f"[conversations] fetched {len(raw)} traces in {time.time() - started:.1f}s")

    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in raw:
        sid = t.get("meta", {}).get(_SESSION_KEY)
        if sid:
            by_scenario[sid].append(t)

    expectations = _scenario_expectations()
    conversations: dict[str, dict[str, Any]] = {}
    for sid, group in by_scenario.items():
        expected_meta = expectations.get(sid, {})
        for run_index, run in enumerate(_split_into_runs(group)):
            key = sid if run_index == 0 else f"{sid}::run{run_index + 1}"
            # cartwheel.user_id lives in the raw metadata blob (normalize_trace
            # keeps it there) even though the curated `meta` dict it also
            # returns does not surface it -- read it straight from `metadata`.
            user_id = run[0].get("metadata", {}).get("cartwheel.user_id")
            run_meta = run[0].get("meta", {})
            store_id = run_meta.get("store")
            store_name = None
            if store_id is not None:
                try:
                    store_name = _store_names().get(int(store_id))
                except (TypeError, ValueError):
                    store_name = None
            conversations[key] = {
                "scenario_id": key,
                "meta": {**run_meta, **expected_meta, "user_id": user_id, "store_name": store_name},
                "turns": [
                    {
                        "trace_id": t["trace_id"],
                        "timestamp": t.get("timestamp"),
                        "messages": t.get("trace") or [],
                        "tool_call_count": t.get("features", {}).get("tool_call_count"),
                    }
                    for t in run
                ],
            }
    return conversations


def _get_conversations(limit: int, refresh: bool) -> tuple[dict[str, dict[str, Any]], str]:
    global _conversation_cache, _conversation_source
    if _conversation_cache is not None and not refresh:
        return _conversation_cache, _conversation_source

    from analysis.helpers import langfuse_io

    if langfuse_io.is_configured():
        _conversation_cache = _live_conversations(limit)
        _conversation_source = "langfuse"
    else:
        _conversation_cache = _offline_conversations()
        _conversation_source = "offline (scenarios/final-results.jsonl)"
    return _conversation_cache, _conversation_source


# ---------------------------------------------------------------------------
# Structured labels: one present/absent judgment per (trace, mode).
# ---------------------------------------------------------------------------


def _label_path(mode: str) -> Path:
    safe = "".join(c for c in mode if c.isalnum() or c in "-_") or "unnamed"
    return LABELS_DIR / f"{safe}.jsonl"


def _read_labels(mode: str) -> dict[str, dict[str, Any]]:
    path = _label_path(mode)
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        tid = rec.get("trace_id")
        if tid:
            out[tid] = rec
    return out


def _write_labels(mode: str, labels: dict[str, dict[str, Any]]) -> None:
    path = _label_path(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(rec, sort_keys=True) for rec in labels.values()]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""))
    tmp.replace(path)


def _all_label_files() -> list[str]:
    if not LABELS_DIR.exists():
        return []
    return sorted(p.stem for p in LABELS_DIR.glob("*.jsonl"))


def _save_label(trace_id: str, mode: str, label: int, note: str | None) -> dict[str, Any]:
    """Upsert one structured judgment locally, then try to sync it to Langfuse.

    Mirrors the reference server's ``_sync_annotation_scores``: Langfuse is
    canonical when configured, the local .jsonl is the committed mirror, and
    a sync failure never loses the local write.
    """
    labels = _read_labels(mode)
    record = {
        "trace_id": trace_id,
        "mode": mode,
        "label": int(label),
        "note": note,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    labels[trace_id] = record
    _write_labels(mode, labels)

    result: dict[str, Any] = {"ok": True, "saved_locally": True}
    if trace_id.startswith("offline::"):
        result["langfuse_synced"] = False
        result["reason"] = "offline trace id, nothing to score in Langfuse"
        return result

    from analysis.helpers import langfuse_io

    if not langfuse_io.is_configured():
        result["langfuse_synced"] = False
        return result
    try:
        langfuse_io.write_label_score(
            trace_id=trace_id, mode=mode, label=int(label), comment=note
        )
        result["langfuse_synced"] = True
    except Exception as exc:  # pragma: no cover - network-only path
        result["langfuse_synced"] = False
        result["error"] = str(exc)
    return result


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json({"error": f"not found: {path.name}"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json({}, status=204)

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_file(UI_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/ui/"):
            asset = UI_DIR / path[len("/ui/"):]
            if asset.is_file() and UI_DIR in asset.resolve().parents:
                self._send_file(asset, _guess_type(asset))
                return

        if path == "/api/conversations":
            # Default high enough to cover this project's full trace count
            # (~450 as of Part A). fetch_traces() sorts by trace_id, which has
            # no relationship to scenario_id, so a lower default silently
            # hides an arbitrary subset of scenarios rather than a clean
            # "first N reviewed" slice -- confirmed the hard way when
            # support-0001 fell outside a limit=150 fetch. The full fetch
            # costs ~8s once per server run (cached after that), which is
            # cheap enough not to default low for.
            limit = int((query.get("limit") or ["1000"])[0])
            refresh = (query.get("refresh") or ["0"])[0] == "1"
            conversations, source = _get_conversations(limit=limit, refresh=refresh)
            self._send_json({"source": source, "conversations": conversations})
            return

        if path == "/api/labels":
            mode = (query.get("mode") or [None])[0]
            if mode:
                self._send_json(list(_read_labels(mode).values()))
            else:
                self._send_json(
                    {m: list(_read_labels(m).values()) for m in _all_label_files()}
                )
            return

        if path == "/api/modes":
            self._send_json(_all_label_files())
            return

        if path == "/api/suggestions_rejected":
            if not REJECTED_SUGGESTIONS_PATH.exists():
                self._send_json([])
                return
            records = [
                json.loads(line) for line in REJECTED_SUGGESTIONS_PATH.read_text().splitlines() if line.strip()
            ]
            self._send_json(records)
            return

        if path == "/api/structural_suggestions":
            # Part E: precomputed present/absent verdicts for the 2 code-check
            # modes, from analysis/apply_structural_checks.py. A suggestion,
            # not a label -- the Labeling grid shows these as a pre-filled
            # cell the student confirms or overrides with a click, same as
            # any other cell. Never written to analysis/state/labels/ here.
            self._send_json(_read_json(STRUCTURAL_SUGGESTIONS_PATH, []))
            return

        if path in API_FILES:
            data = _read_json(API_FILES[path], API_DEFAULTS[path])
            self._send_json(data)
            return

        self._send_json({"error": f"unknown path: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path == "/api/labels":
            data = self._read_body()
            if not isinstance(data, dict):
                self._send_json({"error": "expected a JSON object"}, status=400)
                return
            trace_id = data.get("trace_id")
            mode = data.get("mode")
            label = data.get("label")
            if not trace_id or not mode or label not in (0, 1, "0", "1"):
                self._send_json(
                    {"error": "expected trace_id, mode, and label in (0, 1)"},
                    status=400,
                )
                return
            result = _save_label(str(trace_id), str(mode), int(label), data.get("note"))
            self._send_json(result)
            return

        if path not in API_FILES:
            self._send_json({"error": f"cannot POST to {path}"}, status=404)
            return
        data = self._read_body()
        if data is None:
            self._send_json({"error": "expected a JSON body"}, status=400)
            return
        synced = 0
        if path == "/api/suggestions":
            _log_rejected_suggestions(API_FILES[path], data)
        if path == "/api/annotations":
            try:
                synced = _sync_annotation_scores(data)
            except Exception as exc:  # pragma: no cover - network-only path
                _write_json(API_FILES[path], data)
                self._send_json(
                    {"error": f"Langfuse score write failed: {exc}", "cached_locally": True},
                    status=502,
                )
                return
        _write_json(API_FILES[path], data)
        result = {"ok": True, "count": _count(data)}
        if synced:
            result["langfuse_scores_written"] = synced
        self._send_json(result)


def _count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return len(data)
    return 0


def _annotation_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("annotations", [])
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def _sync_annotation_scores(data: Any) -> int:
    """Same contract as the reference server: score only annotations that
    carry both ``mode`` and a 0/1 ``label`` (free-text-only notes have
    nothing to score against, so they stay local-only)."""
    try:
        from analysis.helpers import langfuse_io
    except Exception:
        return 0
    if not langfuse_io.is_configured():
        return 0

    written = 0
    client = langfuse_io._client()
    for ann in _annotation_list(data):
        trace_id = ann.get("trace_id")
        mode = ann.get("mode")
        label = ann.get("label")
        if not trace_id or not mode or label not in (0, 1, "0", "1"):
            continue
        langfuse_io.write_label_score(
            trace_id=str(trace_id),
            mode=str(mode),
            label=int(label),
            comment=ann.get("note"),
            client=client,
        )
        written += 1
    return written


REJECTED_SUGGESTIONS_PATH = STATE_DIR / "suggestions_rejected.jsonl"
STRUCTURAL_SUGGESTIONS_PATH = STATE_DIR / "structural_suggestions.json"


def _log_rejected_suggestions(suggestions_path: Path, new_data: Any) -> None:
    """Diff the outgoing suggestions POST against what's on disk now, and
    permanently log any suggestion that disappeared without becoming an
    accepted annotation.

    dismissSuggestion() (the UI) just removes an item from the array and
    POSTs what's left -- there is no other record of a dismissal. Without
    this, "Your saved state must contain at least one rejected suggestion"
    (the handout's Part D requirement) can never actually be satisfied: a
    rejected suggestion leaves no trace at all once it's gone. Accepted
    suggestions don't need this treatment -- acceptSuggestion() already
    writes them to annotations.json (with source: "accepted_suggestion")
    before it POSTs the trimmed suggestions array, so we can tell the two
    apart by checking whether that annotation now exists.
    """
    old = _read_json(suggestions_path, [])
    if not isinstance(old, list) or not isinstance(new_data, list):
        return
    new_ids = {s.get("id") for s in new_data if isinstance(s, dict)}
    removed = [s for s in old if isinstance(s, dict) and s.get("id") not in new_ids]
    if not removed:
        return

    annotations = _annotation_list(_read_json(API_FILES["/api/annotations"], []))
    accepted = {
        (a.get("trace_id"), a.get("mode"))
        for a in annotations
        if a.get("source") == "accepted_suggestion"
    }

    rejected = [s for s in removed if (s.get("trace_id"), s.get("mode")) not in accepted]
    if not rejected:
        return
    REJECTED_SUGGESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REJECTED_SUGGESTIONS_PATH.open("a") as f:
        for s in rejected:
            record = {**s, "dismissed_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            f.write(json.dumps(record) + "\n")


def _guess_type(path: Path) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css",
        ".js": "text/javascript",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    }.get(path.suffix, "application/octet-stream")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LABELS_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"review interface on {url}")
    print(f"serving state from {STATE_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
