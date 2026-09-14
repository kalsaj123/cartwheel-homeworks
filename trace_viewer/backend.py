"""Local, read-only trace viewer for the Cartwheel support agent.

Reads traces directly from the local Langfuse API (the Docker Compose stack
in observability/docker-compose.yml). Does not run the agent, does not touch
its SQLite database, and writes nothing back to Langfuse -- purely a viewer
over traces that already exist.

Run with:
    uv run uvicorn trace_viewer.backend:app --port 8020
Then open http://localhost:8020
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from observability.instrument import load_env

load_env()

from langfuse import Langfuse  # noqa: E402 (import after load_env populates os.environ)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Cartwheel trace viewer")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_client: Langfuse | None = None


def _langfuse() -> Langfuse:
    global _client
    if _client is None:
        _client = Langfuse()
    return _client


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _text_of(messages: Any) -> str | None:
    """Pull the first text part out of an OTel GenAI message list, or None."""
    if not messages:
        return None
    try:
        parts = messages[0].get("parts") or []
        for part in parts:
            if part.get("type") == "text":
                return part.get("content")
    except (AttributeError, IndexError, TypeError):
        return None
    return None


def _root_span(observations: list[Any]) -> Any | None:
    for obs in observations or []:
        if obs.name == "cartwheel.session_message":
            return obs
    return None


def _level_str(level: Any) -> str:
    if level is None:
        return "DEFAULT"
    return getattr(level, "value", None) or str(level).rsplit(".", 1)[-1]


def _attrs_of(obs: Any) -> dict[str, Any]:
    return ((obs.metadata or {}).get("attributes")) or {}


def _permalink(t: Any) -> str | None:
    """Build the Langfuse UI URL for this trace, from its html_path."""
    path = getattr(t, "html_path", None)
    if not path:
        return None
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    return host + path


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/traces")
def list_traces() -> list[dict[str, Any]]:
    """Summary rows for the sidebar list. One Langfuse API round trip per page."""
    client = _langfuse()
    summaries: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = client.api.trace.list(page=page, limit=100)
        for t in resp.data:
            # trace.list()'s `observations` field is just a list of id strings;
            # only trace.get() returns full observation objects with attributes.
            full = client.api.trace.get(t.id)
            observations = full.observations or []
            root = _root_span(observations)
            attrs = _attrs_of(root) if root else {}
            tools = [o for o in observations if str(o.type).rsplit(".", 1)[-1] == "TOOL"]
            has_denied = any(
                _attrs_of(o).get("cartwheel.permission_denied") == "true" for o in tools
            )
            has_error = any(
                _level_str(o.level) != "DEFAULT" or o.status_message for o in observations
            )
            summaries.append(
                {
                    "trace_id": t.id,
                    "permalink": _permalink(t),
                    "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                    "latency": t.latency,
                    "request_preview": _text_of(t.input),
                    "reply_preview": _text_of(t.output),
                    "user_role": attrs.get("cartwheel.user_role"),
                    "user_id": attrs.get("cartwheel.user_id"),
                    "prompt_version": attrs.get("cartwheel.prompt_version"),
                    "tool_count": len(tools),
                    "has_permission_denied": has_denied,
                    "has_error": has_error,
                }
            )
        if len(resp.data) < 100:
            break
        page += 1
    summaries.sort(key=lambda s: s["timestamp"] or "", reverse=True)
    return summaries


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str) -> dict[str, Any]:
    """Full detail for one trace: every observation, chronologically ordered."""
    client = _langfuse()
    try:
        full = client.api.trace.get(trace_id)
    except Exception as exc:  # the Langfuse SDK raises its own API error types
        raise HTTPException(status_code=404, detail=f"trace not found: {exc}") from exc

    observations = sorted(full.observations or [], key=lambda o: o.start_time or "")
    return {
        "trace_id": full.id,
        "permalink": _permalink(full),
        "timestamp": full.timestamp.isoformat() if full.timestamp else None,
        "latency": full.latency,
        "input": _jsonable(full.input),
        "output": _jsonable(full.output),
        "observations": [
            {
                "id": o.id,
                "parent_observation_id": o.parent_observation_id,
                "type": str(o.type).rsplit(".", 1)[-1],
                "name": o.name,
                "start_time": o.start_time.isoformat() if o.start_time else None,
                "end_time": o.end_time.isoformat() if o.end_time else None,
                "level": _level_str(o.level),
                "status_message": o.status_message,
                "input": _jsonable(o.input),
                "output": _jsonable(o.output),
                "attributes": _attrs_of(o),
            }
            for o in observations
        ],
    }
