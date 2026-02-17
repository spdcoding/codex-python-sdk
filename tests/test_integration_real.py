from __future__ import annotations

import json
import shutil

import pytest

from codex_python_sdk import create_client
from codex_python_sdk.errors import AppServerConnectionError, CodexAgenticError, NotAuthenticatedError

pytestmark = pytest.mark.real


def _require_real_codex() -> None:
    if shutil.which("codex") is None:
        pytest.skip("`codex` command not found; skipping real integration tests.")


def _maybe_skip_runtime_error(exc: Exception) -> None:
    if isinstance(exc, NotAuthenticatedError):
        pytest.skip(f"Codex not authenticated: {exc}")
    if isinstance(exc, AppServerConnectionError):
        pytest.skip(f"Cannot connect to codex app-server: {exc}")
    if isinstance(exc, CodexAgenticError):
        message = str(exc).lower()
        if "reconnecting" in message or "transport" in message or "stream ended" in message:
            pytest.skip(f"Transient codex runtime instability: {exc}")


def test_real_responses_create_returns_assistant_payload():
    _require_real_codex()
    try:
        with create_client(enable_web_search=False) as client:
            result = client.responses_create(
                prompt='Return exactly JSON object {"ok": true}.',
                turn_params={
                    "model": "gpt-5",
                    "effort": "low",
                    "summary": "none",
                    "outputSchema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                },
            )
    except Exception as exc:  # pragma: no cover - runtime environment dependent
        _maybe_skip_runtime_error(exc)
        raise

    payload = json.loads(result.text)
    assert payload == {"ok": True}


def test_real_responses_events_stream_reaches_turn_completed():
    _require_real_codex()
    try:
        with create_client(enable_web_search=False) as client:
            events = list(
                client.responses_events(
                    prompt="Reply with exactly READY.",
                    turn_params={"model": "gpt-5", "effort": "low", "summary": "none"},
                )
            )
    except Exception as exc:  # pragma: no cover - runtime environment dependent
        _maybe_skip_runtime_error(exc)
        raise

    event_types = [event.type for event in events]
    assert "thread/ready" in event_types
    assert "turn/completed" in event_types


def test_real_two_turns_can_resume_same_session():
    _require_real_codex()
    try:
        with create_client(enable_web_search=False) as client:
            first = client.responses_create(
                prompt="Reply with exactly TURN1.",
                turn_params={"model": "gpt-5", "effort": "low", "summary": "none"},
            )
            second = client.responses_create(
                prompt="Reply with exactly TURN2.",
                session_id=first.session_id,
                turn_params={"model": "gpt-5", "effort": "low", "summary": "none"},
            )
    except Exception as exc:  # pragma: no cover - runtime environment dependent
        _maybe_skip_runtime_error(exc)
        raise

    assert first.session_id
    assert second.session_id == first.session_id
    assert "TURN2" in second.text


def test_real_responses_create_include_events_contains_completed():
    _require_real_codex()
    try:
        with create_client(enable_web_search=False) as client:
            result = client.responses_create(
                prompt="Reply with exactly EVENTS_OK.",
                include_events=True,
                turn_params={"model": "gpt-5", "effort": "low", "summary": "none"},
            )
    except Exception as exc:  # pragma: no cover - runtime environment dependent
        _maybe_skip_runtime_error(exc)
        raise

    assert result.events is not None
    event_types = [event.type for event in result.events]
    assert "thread/ready" in event_types
    assert "turn/completed" in event_types
