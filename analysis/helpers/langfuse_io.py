"""Langfuse I/O for the error-analysis skill.

This is the live wiring behind the skill's documented design (SKILL.md,
"First half: discovery"): the labels live in Langfuse as *scores* against the
project's score configs, the traces come from the Langfuse store, and the
review interface reads pending items from the Langfuse annotation queues.

The whole module is gated behind the ``LANGFUSE_*`` environment variables via
:func:`is_configured`. When Langfuse is not configured, callers fall back to
the committed ``analysis/state/*.json`` files, so the offline demo and the
unit tests never make a network call. Every public function here first checks
``is_configured()`` and raises a clear :class:`LangfuseNotConfigured` if it is
called without the env in place, so a test that reaches this module by
accident fails loudly rather than hanging on a socket.

Trace identifier convention. Live Module 1 traces retain their 32 character
Langfuse identifiers throughout Module 2, so a score is written to the trace
the reviewer inspected. The committed demonstration uses readable identifiers
such as ``store-0000`` and maps them deterministically with
``lf.create_trace_id(seed=<logical id>)`` when a demonstration record is
synchronized to Langfuse.

The Langfuse SDK is imported lazily inside the functions so importing this
module (which ``tools.py`` and ``scale.py`` do at import time) never drags in
the SDK or touches the network in an offline run.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .normalization import normalize_trace

# Optional tag used by the committed demonstration seed. Module 1 scenario
# traces are selected by their scenario metadata instead.
SEED_TAG = "error-analysis-seed"

# The metadata key that carries the readable logical id on each Langfuse trace.
LOGICAL_ID_KEY = "cartwheel_trace_id"


class LangfuseNotConfigured(RuntimeError):
    """Raised when a live Langfuse call is attempted without the env in place.

    A distinct type so a caller (or a test) can tell "Langfuse is off, use the
    local JSON" apart from a real Langfuse error.
    """


def is_configured() -> bool:
    """True when the ``LANGFUSE_*`` env is present, so live calls are allowed.

    Every other function in this module short-circuits on this. Callers use it
    to choose between the live Langfuse path and the committed-JSON fallback,
    and it is the single gate that keeps unit tests offline.
    """
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
        and os.environ.get("LANGFUSE_HOST")
    )


def _client() -> Any:
    """Return an authenticated Langfuse client, or raise if not configured.

    The import is local so this module stays importable (and offline) without
    the SDK installed or the env set.
    """
    if not is_configured():
        raise LangfuseNotConfigured(
            "Langfuse is not configured (LANGFUSE_PUBLIC_KEY / "
            "LANGFUSE_SECRET_KEY / LANGFUSE_HOST). Callers should fall back to "
            "the local analysis/state JSON instead of calling this module."
        )
    from langfuse import Langfuse

    return Langfuse()


def logical_to_langfuse_id(logical_id: str, client: Any | None = None) -> str:
    """Map a readable logical id (``store-0000``) to its Langfuse 32-hex id.

    Deterministic: the same logical id always yields the same Langfuse id, so
    scores and traces line up across separate runs without a lookup table.
    """
    if re.fullmatch(r"[0-9a-fA-F]{32}", logical_id):
        return logical_id.lower()
    lf = client or _client()
    return lf.create_trace_id(seed=logical_id)


# ---------------------------------------------------------------------------
# fetch traces
# ---------------------------------------------------------------------------


def fetch_traces(
    tag: str | None = None,
    limit: int = 1000,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Pull full traces from Langfuse and normalize them for Module 2.

    The original Langfuse identifier remains the trace identifier, so labels
    written as scores preserve the correct join. Scenario identifiers remain
    metadata and are not substituted for the Langfuse identifier.

    Args:
        tag: optionally restrict traces to a Langfuse tag. With no tag, retain
            Module 1 traces carrying `cartwheel.scenario_id` metadata.
        limit: maximum number of traces to pull (paginated under the hood).
        client: an existing Langfuse client (tests/seeds reuse one).

    Returns:
        Normalized trace dictionaries sorted by Langfuse identifier. Each
        record retains its timestamp, observed models, trace input and output,
        and the observation fields needed by later monitoring jobs.
    """
    lf = client or _client()
    tags = [tag] if tag else None

    collected: list[Any] = []
    page = 1
    page_size = 100
    while len(collected) < limit:
        resp = lf.api.trace.list(
            tags=tags,
            page=page,
            limit=min(page_size, limit - len(collected)),
        )
        batch = list(resp.data or [])
        collected.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    # Fetch complete records because list responses do not reliably include
    # observations, tool results, or full inputs and outputs.
    seen: dict[str, dict[str, Any]] = {}
    for summary in collected:
        full = lf.api.trace.get(summary.id)
        trace_id = str(full.id)
        if trace_id in seen:
            continue
        try:
            normalized = normalize_trace(full)
        except ValueError:
            # A trace with no renderable messages or segments (e.g. an ad hoc
            # CLI run with no tool calls and a missing output field) has
            # nothing this module can use anyway -- it would be filtered out
            # by the scenario_id check below regardless. Skip it rather than
            # letting one malformed trace crash every other trace's fetch.
            continue
        if tag is None and not normalized.get("meta", {}).get("scenario_id"):
            continue
        seen[trace_id] = normalized
    out = list(seen.values())
    out.sort(key=lambda trace: trace["trace_id"])
    return out


# ---------------------------------------------------------------------------
# annotation queues
# ---------------------------------------------------------------------------


def ensure_score_config(
    name: str, client: Any | None = None
) -> str:
    """Get (or create) a NUMERIC 0..1 score config named ``name``.

    A queue collects labels against score configs, and the failure-mode label
    is a numeric failure indicator (1 = failure present). This returns the config id, creating
    the config on first use so ``ensure_queue`` is self-contained.
    """
    lf = client or _client()
    # Look for an existing config by name (paginated).
    page = 1
    while True:
        resp = lf.api.score_configs.get(page=page, limit=100)
        for cfg in resp.data or []:
            if cfg.name == name:
                return cfg.id
        if not resp.data or len(resp.data) < 100:
            break
        page += 1

    from langfuse.api.resources.score_configs.types.create_score_config_request import (
        CreateScoreConfigRequest,
    )
    from langfuse.api.resources.commons.types.score_data_type import ScoreDataType

    req = CreateScoreConfigRequest(
        name=name,
        dataType=ScoreDataType.NUMERIC,
        minValue=0,
        maxValue=1,
        description="Mode label stored as 1 when the failure is present for the "
        "error-analysis skill.",
    )
    cfg = lf.api.score_configs.create(request=req)
    return cfg.id


def ensure_queue(
    name: str,
    score_config_ids: list[str] | None = None,
    client: Any | None = None,
) -> str:
    """Get the annotation queue named ``name``, creating it if absent.

    The review interface reads pending items from a Langfuse annotation queue
    (SKILL.md phase 1a). This returns the queue id, so a caller can enqueue
    traces and read pending items without caring whether the queue already
    existed.

    Args:
        name: the queue name (unique per project).
        score_config_ids: score-config ids the queue collects labels against.
            Langfuse requires at least one; when none is passed, a default
            NUMERIC 0..1 config named after the queue is created and used.
        client: an existing Langfuse client.

    Returns:
        The queue id.
    """
    lf = client or _client()
    existing = _find_queue(lf, name)
    if existing is not None:
        return existing

    config_ids = score_config_ids or [ensure_score_config(name, client=lf)]

    from langfuse.api.resources.annotation_queues.types.create_annotation_queue_request import (
        CreateAnnotationQueueRequest,
    )

    req = CreateAnnotationQueueRequest(
        name=name,
        description="Error-analysis review queue (Cartwheel Module 2 skill).",
        scoreConfigIds=config_ids,
    )
    queue = lf.api.annotation_queues.create_queue(request=req)
    return queue.id


def _find_queue(lf: Any, name: str) -> str | None:
    """Return the id of the queue named ``name``, or ``None`` if none exists."""
    page = 1
    while True:
        resp = lf.api.annotation_queues.list_queues(page=page, limit=100)
        for q in resp.data or []:
            if q.name == name:
                return q.id
        if not resp.data or len(resp.data) < 100:
            return None
        page += 1


def enqueue_trace(
    queue_id: str, logical_id: str, client: Any | None = None
) -> str:
    """Add one trace (by logical id) to an annotation queue as a pending item.

    Returns the created queue-item id. Used by the seed and by
    ``select_traces`` so the human sees the selected batch as pending work in
    the review interface.
    """
    lf = client or _client()
    langfuse_id = logical_to_langfuse_id(logical_id, lf)

    from langfuse.api.resources.annotation_queues.types.create_annotation_queue_item_request import (
        CreateAnnotationQueueItemRequest,
    )
    from langfuse.api.resources.annotation_queues.types.annotation_queue_object_type import (
        AnnotationQueueObjectType,
    )

    req = CreateAnnotationQueueItemRequest(
        objectId=langfuse_id,
        objectType=AnnotationQueueObjectType.TRACE,
    )
    item = lf.api.annotation_queues.create_queue_item(queue_id=queue_id, request=req)
    return item.id


def pending_queue_items(
    queue_id: str, limit: int = 1000, client: Any | None = None
) -> list[dict[str, str]]:
    """Read the pending items of an annotation queue.

    Returns ``{"item_id", "object_id"}`` dicts for each item still awaiting a
    label (status ``PENDING``). The ``object_id`` is the Langfuse trace id; a
    caller that wants the logical id can join it against :func:`fetch_traces`.

    Args:
        queue_id: the queue to read.
        limit: maximum items to return.
        client: an existing Langfuse client.
    """
    lf = client or _client()
    from langfuse.api.resources.annotation_queues.types.annotation_queue_status import (
        AnnotationQueueStatus,
    )

    out: list[dict[str, str]] = []
    page = 1
    while len(out) < limit:
        resp = lf.api.annotation_queues.list_queue_items(
            queue_id=queue_id,
            status=AnnotationQueueStatus.PENDING,
            page=page,
            limit=min(100, limit - len(out)),
        )
        batch = list(resp.data or [])
        for item in batch:
            out.append({"item_id": item.id, "object_id": item.object_id})
        if len(batch) < 100:
            break
        page += 1
    return out


# ---------------------------------------------------------------------------
# labels as scores
# ---------------------------------------------------------------------------


def write_label_score(
    trace_id: str,
    mode: str,
    label: int,
    comment: str | None = None,
    client: Any | None = None,
) -> str:
    """Write a human label back to Langfuse as a numeric score.

    This realizes "labels live in Langfuse as scores" (SKILL.md): a label for
    a failure mode is a score named after the mode, valued 0 or 1 on the
    stored mode convention (1 = failure present), attached to the trace.

    Args:
        trace_id: a Langfuse trace identifier, or a logical demonstration
            identifier mapped deterministically for the offline seed.
        mode: the failure mode, used as the score name (so one trace can carry
            one score per mode).
        label: 0 or 1.
        comment: optional free-text note stored alongside the score.
        client: an existing Langfuse client.

    Returns:
        The Langfuse trace id the score was written against.
    """
    lf = client or _client()
    langfuse_id = logical_to_langfuse_id(trace_id, lf)
    lf.create_score(
        name=mode,
        value=int(label),
        trace_id=langfuse_id,
        data_type="NUMERIC",
        comment=comment,
    )
    lf.flush()
    return langfuse_id


def read_label_scores(
    trace_id: str, mode: str | None = None, client: Any | None = None
) -> list[dict[str, Any]]:
    """Read back the label scores on a trace (for verification and sync).

    Args:
        trace_id: the *logical* trace id.
        mode: restrict to scores with this name (the mode); ``None`` returns
            all scores on the trace.
        client: an existing Langfuse client.

    Returns:
        ``{"name", "value", "comment"}`` dicts, one per matching score.
    """
    lf = client or _client()
    langfuse_id = logical_to_langfuse_id(trace_id, lf)
    resp = lf.api.score_v_2.get(trace_id=langfuse_id)
    out: list[dict[str, Any]] = []
    for s in resp.data or []:
        if mode is not None and s.name != mode:
            continue
        out.append(
            {
                "name": s.name,
                "value": getattr(s, "value", None),
                "comment": getattr(s, "comment", None),
            }
        )
    return out
