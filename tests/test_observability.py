"""Homework 2, Part D: authentication tests.

These exercise the session/token logic directly, as plain function calls.
No Langfuse, Docker, or model provider key is required or used.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server import app as server_app


def test_create_session_rejects_role_mismatch() -> None:
    """A claimed role that does not match the user's stored role is rejected."""
    server_app._SESSIONS.clear()
    with pytest.raises(HTTPException) as exc_info:
        server_app.create_session(
            server_app.SessionCreate(user_id=1, role="merchant")  # user 1 is a shopper
        )
    assert exc_info.value.status_code == 403


def test_token_cannot_authorize_a_different_session() -> None:
    """A token issued for one session must not authorize a different session."""
    server_app._SESSIONS.clear()
    session_a = server_app.create_session(
        server_app.SessionCreate(user_id=1, role="shopper")
    )
    session_b = server_app.create_session(
        server_app.SessionCreate(user_id=9002, role="merchant")
    )
    assert session_a["session_id"] != session_b["session_id"]

    with pytest.raises(HTTPException) as exc_info:
        server_app._authorize(session_b["session_id"], f"Bearer {session_a['token']}")
    assert exc_info.value.status_code == 403
