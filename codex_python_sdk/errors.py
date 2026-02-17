from __future__ import annotations


class CodexAgenticError(RuntimeError):
    """Base error for all client failures."""


class AppServerConnectionError(CodexAgenticError):
    """Raised when app-server transport/session setup or request execution fails."""


class SessionNotFoundError(CodexAgenticError):
    """Raised when the requested session id is unknown."""


class NotAuthenticatedError(CodexAgenticError):
    """Raised when Codex CLI authentication is unavailable or invalid."""

