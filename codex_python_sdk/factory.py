from __future__ import annotations

from typing import Any

from .async_client import AsyncCodexAgenticClient
from .sync_client import CodexAgenticClient


def create_client(**kwargs: Any) -> CodexAgenticClient:
    """Create a synchronous client.

    Keyword args are forwarded to :class:`AsyncCodexAgenticClient`.
    """

    return CodexAgenticClient(**kwargs)


def create_async_client(**kwargs: Any) -> AsyncCodexAgenticClient:
    """Create an asynchronous client.

    Keyword args are forwarded to :class:`AsyncCodexAgenticClient`.
    """

    return AsyncCodexAgenticClient(**kwargs)

