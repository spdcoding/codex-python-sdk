from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._shared import utc_now


@dataclass
class AgentResponse:
    """Aggregated final response for one prompt execution.

    Attributes:
        text: Final assistant text (message completion or merged deltas).
        session_id: Thread id used by app-server.
        request_id: Optional request id extracted from notifications.
        tool_name: Backend label, currently always ``"app-server"``.
        raw: Raw payload from the last observed event.
        events: Optional full event list when ``include_events=True`` is used.
    """

    text: str
    session_id: str
    request_id: str | None
    tool_name: str
    raw: Any
    events: list["ResponseEvent"] | None = None


@dataclass
class ResponseEvent:
    """Normalized streaming event produced from app-server notifications."""

    type: str
    phase: str
    text_delta: str | None = None
    message_text: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    summary_index: int | None = None
    thread_name: str | None = None
    token_usage: dict[str, Any] | None = None
    plan: list[dict[str, Any]] | None = None
    diff: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: utc_now())
