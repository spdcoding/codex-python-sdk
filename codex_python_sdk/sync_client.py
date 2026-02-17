from __future__ import annotations

import asyncio
import threading
from typing import Any, Iterator

from .async_client import AsyncCodexAgenticClient
from .errors import CodexAgenticError
from .types import AgentResponse, ResponseEvent


class CodexAgenticClient:
    """Synchronous facade over :class:`AsyncCodexAgenticClient`.

    This wrapper owns an internal event loop and exposes blocking APIs for
    scripts and notebooks that prefer non-async usage.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._loop = asyncio.new_event_loop()
        self._client = AsyncCodexAgenticClient(**kwargs)
        self._loop_started = threading.Event()
        self._loop_thread = threading.Thread(target=self._loop_thread_main, name="codex-agentic-client-loop", daemon=True)
        self._loop_thread.start()
        self._loop_started.wait()
        self._closed = False

    def _loop_thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_started.set()
        self._loop.run_forever()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._run(self._client.close())
        finally:
            try:
                if not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._loop.stop)
            finally:
                self._loop_thread.join(timeout=2.0)
                # If the loop thread didn't exit, we cannot safely close the loop here.
                if (not self._loop_thread.is_alive()) and (not self._loop.is_running()) and (not self._loop.is_closed()):
                    self._loop.close()
                self._closed = True

    def __enter__(self) -> "CodexAgenticClient":
        try:
            self._run(self._client.connect())
        except Exception:
            try:
                self.close()
            except Exception:
                # Preserve the original connection failure.
                pass
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _run(self, coro: Any) -> Any:
        if self._closed:
            raise CodexAgenticError("Client is closed.")
        if threading.current_thread() is self._loop_thread:
            raise CodexAgenticError("Cannot block on sync client from its own loop thread.")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    def thread_start(
        self,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new thread."""

        return self._run(self._client.thread_start(params=params))

    def thread_read(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        """Read one thread by id."""

        return self._run(self._client.thread_read(thread_id, include_turns=include_turns))

    def thread_list(self, limit: int = 50, *, sort_key: str = "updated_at") -> list[dict[str, Any]]:
        """List available threads."""

        return self._run(self._client.thread_list(limit=limit, sort_key=sort_key))

    def thread_archive(self, thread_id: str) -> dict[str, Any]:
        """Archive one thread."""

        return self._run(self._client.thread_archive(thread_id))

    def thread_fork(
        self,
        thread_id: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._run(self._client.thread_fork(thread_id, params=params))

    def thread_name_set(self, thread_id: str, name: str) -> dict[str, Any]:
        return self._run(self._client.thread_name_set(thread_id, name))

    def thread_unarchive(self, thread_id: str) -> dict[str, Any]:
        return self._run(self._client.thread_unarchive(thread_id))

    def thread_compact_start(self, thread_id: str) -> dict[str, Any]:
        return self._run(self._client.thread_compact_start(thread_id))

    def thread_rollback(self, thread_id: str, num_turns: int) -> dict[str, Any]:
        return self._run(self._client.thread_rollback(thread_id, num_turns))

    def thread_loaded_list(self, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        return self._run(self._client.thread_loaded_list(limit=limit, cursor=cursor))

    def skills_list(
        self,
        *,
        cwds: list[str] | None = None,
        force_reload: bool | None = None,
    ) -> dict[str, Any]:
        return self._run(self._client.skills_list(cwds=cwds, force_reload=force_reload))

    def skills_remote_read(self) -> dict[str, Any]:
        return self._run(self._client.skills_remote_read())

    def skills_remote_write(self, hazelnut_id: str, is_preload: bool) -> dict[str, Any]:
        return self._run(self._client.skills_remote_write(hazelnut_id, is_preload))

    def app_list(self, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        return self._run(self._client.app_list(limit=limit, cursor=cursor))

    def skills_config_write(self, path: str, enabled: bool) -> dict[str, Any]:
        return self._run(self._client.skills_config_write(path, enabled))

    def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return self._run(self._client.turn_interrupt(thread_id, turn_id))

    def turn_steer(self, thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        return self._run(self._client.turn_steer(thread_id, turn_id, prompt))

    def review_start(
        self,
        thread_id: str,
        target: dict[str, Any],
        *,
        delivery: str | None = None,
    ) -> dict[str, Any]:
        return self._run(self._client.review_start(thread_id, target, delivery=delivery))

    def model_list(self, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        return self._run(self._client.model_list(limit=limit, cursor=cursor))

    def account_rate_limits_read(self) -> dict[str, Any]:
        return self._run(self._client.account_rate_limits_read())

    def account_read(self, *, refresh_token: bool | None = None) -> dict[str, Any]:
        return self._run(self._client.account_read(refresh_token=refresh_token))

    def command_exec(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
        sandbox_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._run(
            self._client.command_exec(
                command,
                cwd=cwd,
                timeout_ms=timeout_ms,
                sandbox_policy=sandbox_policy,
            )
        )

    def config_read(self, *, cwd: str | None = None, include_layers: bool = False) -> dict[str, Any]:
        return self._run(self._client.config_read(cwd=cwd, include_layers=include_layers))

    def config_value_write(
        self,
        key_path: str,
        value: Any,
        *,
        merge_strategy: str = "upsert",
        file_path: str | None = None,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        return self._run(
            self._client.config_value_write(
                key_path,
                value,
                merge_strategy=merge_strategy,
                file_path=file_path,
                expected_version=expected_version,
            )
        )

    def config_batch_write(
        self,
        edits: list[dict[str, Any]],
        *,
        file_path: str | None = None,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        return self._run(
            self._client.config_batch_write(
                edits,
                file_path=file_path,
                expected_version=expected_version,
            )
        )

    def config_requirements_read(self) -> dict[str, Any]:
        return self._run(self._client.config_requirements_read())

    def config_mcp_server_reload(self) -> dict[str, Any]:
        return self._run(self._client.config_mcp_server_reload())

    def mcp_server_status_list(self, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        return self._run(self._client.mcp_server_status_list(limit=limit, cursor=cursor))

    def mcp_server_oauth_login(
        self,
        name: str,
        *,
        scopes: list[str] | None = None,
        timeout_secs: int | None = None,
    ) -> dict[str, Any]:
        return self._run(self._client.mcp_server_oauth_login(name, scopes=scopes, timeout_secs=timeout_secs))

    def fuzzy_file_search(
        self,
        query: str,
        *,
        roots: list[str] | None = None,
        cancellation_token: str | None = None,
    ) -> dict[str, Any]:
        return self._run(
            self._client.fuzzy_file_search(
                query,
                roots=roots,
                cancellation_token=cancellation_token,
            )
        )

    def responses_events(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        thread_params: dict[str, Any] | None = None,
        turn_params: dict[str, Any] | None = None,
    ) -> Iterator[ResponseEvent]:
        """Run one prompt and yield structured events."""

        async_iter = self._client.responses_events(
            prompt=prompt,
            session_id=session_id,
            thread_params=thread_params,
            turn_params=turn_params,
        )
        try:
            while True:
                try:
                    event = self._run(async_iter.__anext__())
                except StopAsyncIteration:
                    break
                else:
                    yield event
        finally:
            aclose = getattr(async_iter, "aclose", None)
            if callable(aclose):
                try:
                    self._run(aclose())
                except Exception:
                    pass

    def responses_stream_text(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        thread_params: dict[str, Any] | None = None,
        turn_params: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """Run one prompt and yield text deltas only."""

        async_iter = self._client.responses_stream_text(
            prompt=prompt,
            session_id=session_id,
            thread_params=thread_params,
            turn_params=turn_params,
        )
        try:
            while True:
                try:
                    chunk = self._run(async_iter.__anext__())
                except StopAsyncIteration:
                    break
                else:
                    yield chunk
        finally:
            aclose = getattr(async_iter, "aclose", None)
            if callable(aclose):
                try:
                    self._run(aclose())
                except Exception:
                    pass

    def responses_create(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        thread_params: dict[str, Any] | None = None,
        turn_params: dict[str, Any] | None = None,
        include_events: bool = False,
    ) -> AgentResponse:
        """Run one prompt and return final aggregated response."""

        return self._run(
            self._client.responses_create(
                prompt=prompt,
                session_id=session_id,
                thread_params=thread_params,
                turn_params=turn_params,
                include_events=include_events,
            )
        )
