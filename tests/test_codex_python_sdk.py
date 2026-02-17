from __future__ import annotations

import asyncio
import json
import threading
from io import StringIO
from typing import Any

from codex_python_sdk import (
    AgentResponse,
    AsyncCodexAgenticClient,
    CodexAgenticClient,
    CodexAgenticError,
    NotAuthenticatedError,
    ResponseEvent,
    SessionNotFoundError,
    render_exec_style_events,
)
from codex_python_sdk.errors import AppServerConnectionError


def test_to_toml_literal_supports_scalars_and_collections():
    c = AsyncCodexAgenticClient()
    assert c._to_toml_literal(True) == "true"
    assert c._to_toml_literal(3) == "3"
    assert c._to_toml_literal("x") == '"x"'
    assert c._to_toml_literal(["a", 2]) == '["a", 2]'
    assert c._to_toml_literal({"k": "v"}) == '{"k" = "v"}'


def test_to_toml_literal_escapes_backslash_and_control_chars():
    c = AsyncCodexAgenticClient()
    assert c._to_toml_literal(r"C:\repo\name") == json.dumps(r"C:\repo\name")
    assert c._to_toml_literal("line1\nline2\tend") == json.dumps("line1\nline2\tend")


def test_build_server_args_uses_app_server_and_overrides():
    c = AsyncCodexAgenticClient(
        app_server_args=["app-server"],
        enable_web_search=True,
        server_config_overrides={"model": "gpt-5.3-codex"},
    )
    args = c._build_server_args()
    assert args[0] == "app-server"
    assert "--enable" in args and "web_search" in args
    assert "-c" in args
    assert 'model="gpt-5.3-codex"' in args


def test_build_server_args_web_search_is_enabled_by_default():
    c = AsyncCodexAgenticClient(app_server_args=["app-server"])
    args = c._build_server_args()
    assert args == ["app-server", "--enable", "web_search"]


def test_build_server_args_supports_disabling_web_search():
    c = AsyncCodexAgenticClient(app_server_args=["app-server"], enable_web_search=False)
    args = c._build_server_args()
    assert args == ["app-server"]


def test_explicit_empty_env_is_preserved():
    c = AsyncCodexAgenticClient(env={})
    assert c.env == {}


def test_async_client_init_does_not_create_asyncio_primitives_eagerly(monkeypatch):
    def _unexpected(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("asyncio primitive should not be created during __init__")

    monkeypatch.setattr("codex_python_sdk.async_client.asyncio.Lock", _unexpected)
    monkeypatch.setattr("codex_python_sdk.async_client.asyncio.Queue", _unexpected)
    monkeypatch.setattr("codex_python_sdk.async_client.asyncio.Condition", _unexpected)

    client = AsyncCodexAgenticClient()
    assert client._connect_lock_ref is None
    assert client._write_lock_ref is None
    assert client._notification_queue_ref is None
    assert client._notification_condition_ref is None


def test_async_client_initializes_primitives_lazily_in_running_loop():
    client = AsyncCodexAgenticClient()

    async def _run() -> None:
        _ = client._connect_lock
        _ = client._write_lock
        _ = client._notification_queue
        _ = client._notification_condition

    asyncio.run(_run())
    assert client._connect_lock_ref is not None
    assert client._write_lock_ref is not None
    assert client._notification_queue_ref is not None
    assert client._notification_condition_ref is not None


def test_removed_reconnect_attempts_kwarg_raises_type_error():
    try:
        AsyncCodexAgenticClient(reconnect_attempts=1)  # type: ignore[call-arg]
        raise AssertionError("expected TypeError")
    except TypeError as exc:
        assert "reconnect_attempts" in str(exc)


def test_raise_rpc_error_maps_auth_and_not_found():
    try:
        AsyncCodexAgenticClient._raise_rpc_error({"message": "unauthorized: please login"})
        raise AssertionError("expected NotAuthenticatedError")
    except NotAuthenticatedError as exc:
        text = str(exc)
        assert "codex login" in text
        assert "forced_login_method" not in text

    try:
        AsyncCodexAgenticClient._raise_rpc_error({"message": "thread not found"})
        raise AssertionError("expected SessionNotFoundError")
    except SessionNotFoundError:
        pass


def test_raise_connection_error_maps_auth_to_not_authenticated():
    try:
        AsyncCodexAgenticClient._raise_connection_error(RuntimeError("auth failed"))
        raise AssertionError("expected NotAuthenticatedError")
    except NotAuthenticatedError as exc:
        text = str(exc)
        assert "codex login" in text
        assert "forced_login_method" not in text


def test_raise_connection_error_non_auth_stays_connection_error():
    try:
        AsyncCodexAgenticClient._raise_connection_error(RuntimeError("broken pipe"))
        raise AssertionError("expected AppServerConnectionError")
    except AppServerConnectionError as exc:
        assert "broken pipe" in str(exc)


def test_request_cleans_pending_on_send_failure(monkeypatch):
    client = AsyncCodexAgenticClient()

    class _FakeProcess:
        stdin = object()

    client._proc = _FakeProcess()

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        del payload
        raise RuntimeError("write failed")

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run() -> None:
        try:
            await client._request("thread/read", {"threadId": "t1"}, ensure_connected=False)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert str(exc) == "write failed"

    asyncio.run(_run())
    assert client._pending == {}


def test_request_wraps_non_dict_result_in_value(monkeypatch):
    client = AsyncCodexAgenticClient()

    class _FakeProcess:
        stdin = object()

    client._proc = _FakeProcess()

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        req_id = payload["id"]
        fut = client._pending[req_id]
        fut.set_result({"jsonrpc": "2.0", "id": req_id, "result": "ok"})

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run() -> dict[str, Any]:
        return await client._request("thread/read", {"threadId": "t1"}, ensure_connected=False)

    out = asyncio.run(_run())
    assert out == {"value": "ok"}


def test_request_without_timeout_does_not_use_wait_for(monkeypatch):
    client = AsyncCodexAgenticClient()

    class _FakeProcess:
        stdin = object()

    client._proc = _FakeProcess()

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        req_id = payload["id"]
        fut = client._pending[req_id]
        fut.set_result({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})

    async def _unexpected_wait_for(awaitable: Any, timeout: float):
        del awaitable, timeout
        raise AssertionError("asyncio.wait_for should not be used when timeout_seconds is None")

    monkeypatch.setattr(client, "_send_json", _fake_send_json)
    monkeypatch.setattr("codex_python_sdk.async_client.asyncio.wait_for", _unexpected_wait_for)

    async def _run() -> dict[str, Any]:
        return await client._request("thread/read", {"threadId": "t1"}, ensure_connected=False)

    out = asyncio.run(_run())
    assert out == {"ok": True}


def test_request_uses_wait_for_when_timeout_is_explicit(monkeypatch):
    client = AsyncCodexAgenticClient()

    class _FakeProcess:
        stdin = object()

    client._proc = _FakeProcess()

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        req_id = payload["id"]
        fut = client._pending[req_id]
        fut.set_result({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})

    state = {"used": False}
    original_wait_for = asyncio.wait_for

    async def _fake_wait_for(awaitable: Any, timeout: float):
        state["used"] = True
        assert timeout == 0.25
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(client, "_send_json", _fake_send_json)
    monkeypatch.setattr("codex_python_sdk.async_client.asyncio.wait_for", _fake_wait_for)

    async def _run() -> dict[str, Any]:
        return await client._request(
            "thread/read",
            {"threadId": "t1"},
            ensure_connected=False,
            timeout_seconds=0.25,
        )

    out = asyncio.run(_run())
    assert out == {"ok": True}
    assert state["used"] is True


def test_resolve_server_request_rejects_non_dict_handler_result():
    client = AsyncCodexAgenticClient()

    def _bad_handler(params: dict[str, Any]) -> str:
        del params
        return "not-a-dict"

    async def _run() -> None:
        try:
            await client._resolve_server_request(
                "item/commandExecution/requestApproval",
                {"command": "pwd"},
                _bad_handler,
                {"decision": "accept"},
            )
            raise AssertionError("expected CodexAgenticError")
        except CodexAgenticError as exc:
            assert "must return a dict" in str(exc)

    asyncio.run(_run())


def test_notification_to_event_extracts_delta_and_completed_message():
    delta_payload = {
        "method": "item/agentMessage/delta",
        "params": {"threadId": "t1", "turnId": "u1", "delta": "hel"},
    }
    completed_payload = {
        "method": "item/completed",
        "params": {"threadId": "t1", "turnId": "u1", "item": {"type": "agentMessage", "text": "hello"}},
    }
    e1 = AsyncCodexAgenticClient._notification_to_event(delta_payload)
    e2 = AsyncCodexAgenticClient._notification_to_event(completed_payload)

    assert e1.phase == "assistant"
    assert e1.text_delta == "hel"
    assert e1.session_id == "t1"

    assert e2.phase == "system"
    assert e2.message_text == "hello"


def test_notification_to_event_extracts_reasoning_and_tool_text():
    reasoning_payload = {
        "method": "item/reasoning/summaryTextDelta",
        "params": {"threadId": "t1", "turnId": "u1", "delta": "thinking..."},
    }
    mcp_progress_payload = {
        "method": "item/mcpToolCall/progress",
        "params": {"threadId": "t1", "turnId": "u1", "message": "searching docs"},
    }

    e1 = AsyncCodexAgenticClient._notification_to_event(reasoning_payload)
    e2 = AsyncCodexAgenticClient._notification_to_event(mcp_progress_payload)

    assert e1.text_delta == "thinking..."
    assert e1.message_text == "thinking..."
    assert e2.message_text == "searching docs"


def test_render_exec_style_events_outputs_compact_layout():
    sink = StringIO()
    events = iter(
        [
            ResponseEvent(type="thread/ready", phase="system", session_id="thread-1", raw={"params": {}}),
            ResponseEvent(
                type="item/reasoning/summaryTextDelta",
                phase="planning",
                text_delta="plan...",
                raw={"params": {"delta": "plan..."}},
            ),
            ResponseEvent(
                type="item/agentMessage/delta",
                phase="assistant",
                text_delta="hello",
                raw={"params": {"delta": "hello"}},
            ),
            ResponseEvent(type="turn/completed", phase="completed", raw={"params": {}}),
        ]
    )

    render_exec_style_events(events, stream=sink)
    out = sink.getvalue()
    assert "Session: thread-1" in out
    assert "[thinking] plan..." in out
    assert "Assistant: hello" in out
    assert "[summary] status=completed, tools=0, errors=0" in out
    assert "== Turn Completed ==" in out


def test_render_exec_style_events_formats_tool_lifecycle():
    sink = StringIO()
    events = iter(
        [
            ResponseEvent(
                type="item/started",
                phase="tool",
                raw={
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "id": "it-1",
                            "command": "ls -la",
                        }
                    }
                },
            ),
            ResponseEvent(
                type="item/commandExecution/outputDelta",
                phase="tool",
                text_delta="file.txt\n",
                raw={"params": {"itemId": "it-1", "delta": "file.txt\n"}},
            ),
            ResponseEvent(
                type="item/completed",
                phase="tool",
                raw={
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "status": "completed",
                            "exitCode": 0,
                            "durationMs": 12,
                        }
                    }
                },
            ),
            ResponseEvent(
                type="turn/completed",
                phase="completed",
                raw={"params": {"turn": {"status": "completed"}}},
            ),
        ]
    )

    render_exec_style_events(events, stream=sink)
    out = sink.getvalue()
    assert "[tool.exec] ls -la" in out
    assert "[tool.output:exec]" in out
    assert "file.txt" in out
    assert "[tool.exec.done] completed, exit=0, 12ms" in out
    assert "[summary] status=completed, tools=1, errors=0" in out


def test_render_exec_style_events_suppresses_duplicate_codex_events_by_default():
    sink = StringIO()
    events = iter(
        [
            ResponseEvent(
                type="codex/event/reasoning_content_delta",
                phase="planning",
                text_delta="dup",
                raw={"params": {"delta": "dup"}},
            ),
            ResponseEvent(
                type="item/reasoning/summaryTextDelta",
                phase="planning",
                text_delta="real",
                raw={"params": {"delta": "real"}},
            ),
        ]
    )

    render_exec_style_events(events, stream=sink)
    out = sink.getvalue()
    assert "[thinking] real" in out
    assert "dup" not in out


def test_render_exec_style_events_deduplicates_deprecation_warnings():
    sink = StringIO()
    events = iter(
        [
            ResponseEvent(
                type="codex/event/deprecation_notice",
                phase="system",
                message_text="deprecation_notice",
                raw={"params": {"method": "thread/resume"}},
            ),
            ResponseEvent(
                type="deprecationNotice",
                phase="system",
                message_text="deprecationNotice",
                raw={"params": {"method": "thread/resume"}},
            ),
        ]
    )

    render_exec_style_events(events, stream=sink, color="off")
    out = sink.getvalue()
    assert out.count("[warning]") == 1
    assert "thread/resume is deprecated" in out


def test_render_exec_style_events_suppresses_non_actionable_deprecation_warnings():
    sink = StringIO()
    events = iter(
        [
            ResponseEvent(
                type="deprecationNotice",
                phase="system",
                message_text="deprecationNotice",
                raw={"params": {}},
            )
        ]
    )

    render_exec_style_events(events, stream=sink, color="off")
    assert sink.getvalue() == ""


def test_render_exec_style_events_color_vivid_emits_ansi():
    sink = StringIO()
    events = iter([ResponseEvent(type="thread/ready", phase="system", session_id="thread-1", raw={"params": {}})])
    render_exec_style_events(events, stream=sink, color="vivid")
    out = sink.getvalue()
    assert "\x1b[" in out
    assert "Session:" in out


def test_render_exec_style_events_color_off_has_no_ansi():
    sink = StringIO()
    events = iter([ResponseEvent(type="thread/ready", phase="system", session_id="thread-1", raw={"params": {}})])
    render_exec_style_events(events, stream=sink, color="off")
    out = sink.getvalue()
    assert "\x1b[" not in out
    assert "Session:" in out


def test_responses_create_aggregates_from_event_stream(monkeypatch):
    client = AsyncCodexAgenticClient()

    async def _fake_events(**_: Any):
        yield ResponseEvent(type="thread/ready", phase="system", session_id="thread-1")
        yield ResponseEvent(type="item/agentMessage/delta", phase="assistant", session_id="thread-1", text_delta="he")
        yield ResponseEvent(type="item/agentMessage/delta", phase="assistant", session_id="thread-1", text_delta="llo")
        yield ResponseEvent(type="turn/completed", phase="completed", session_id="thread-1")

    monkeypatch.setattr(client, "responses_events", _fake_events)

    async def _run() -> AgentResponse:
        return await client.responses_create(prompt="x")

    resp = asyncio.run(_run())
    assert resp.session_id == "thread-1"
    assert resp.text == "hello"
    assert resp.tool_name == "app-server"
    assert resp.events is None


def test_responses_create_can_include_full_events(monkeypatch):
    client = AsyncCodexAgenticClient()

    async def _fake_events(**_: Any):
        yield ResponseEvent(type="thread/ready", phase="system", session_id="thread-1")
        yield ResponseEvent(type="item/agentMessage/delta", phase="assistant", session_id="thread-1", text_delta="ok")
        yield ResponseEvent(type="turn/completed", phase="completed", session_id="thread-1")

    monkeypatch.setattr(client, "responses_events", _fake_events)

    async def _run() -> AgentResponse:
        return await client.responses_create(prompt="x", include_events=True)

    resp = asyncio.run(_run())
    assert resp.events is not None
    assert [event.type for event in resp.events] == [
        "thread/ready",
        "item/agentMessage/delta",
        "turn/completed",
    ]


def test_responses_create_prefers_assistant_message_over_system_updates(monkeypatch):
    client = AsyncCodexAgenticClient()

    async def _fake_events(**_: Any):
        yield ResponseEvent(type="thread/ready", phase="system", session_id="thread-1")
        yield ResponseEvent(type="item/agentMessage/delta", phase="assistant", session_id="thread-1", text_delta="{")
        yield ResponseEvent(
            type="codex/event/agent_message",
            phase="system",
            session_id="thread-1",
            message_text='{"decision":"accept","reason":"ok"}',
        )
        yield ResponseEvent(
            type="thread/tokenUsage/updated",
            phase="system",
            session_id="thread-1",
            message_text="last=123,total=456",
        )
        yield ResponseEvent(type="turn/completed", phase="completed", session_id="thread-1")

    monkeypatch.setattr(client, "responses_events", _fake_events)

    async def _run() -> AgentResponse:
        return await client.responses_create(prompt="x")

    resp = asyncio.run(_run())
    assert resp.text == '{"decision":"accept","reason":"ok"}'


def test_sync_responses_events_wraps_async_generator(monkeypatch):
    client = CodexAgenticClient()

    async def _fake_events(**_: Any):
        yield ResponseEvent(type="thread/ready", phase="system", session_id="thread-1")
        yield ResponseEvent(type="turn/completed", phase="completed", session_id="thread-1")

    monkeypatch.setattr(client._client, "responses_events", _fake_events)

    events = list(client.responses_events(prompt="x"))
    assert [e.type for e in events] == ["thread/ready", "turn/completed"]
    client.close()


def test_sync_responses_stream_text_wraps_async_generator(monkeypatch):
    client = CodexAgenticClient()

    async def _fake_stream(**_: Any):
        yield "a"
        yield "b"

    monkeypatch.setattr(client._client, "responses_stream_text", _fake_stream)

    chunks = list(client.responses_stream_text(prompt="x"))
    assert chunks == ["a", "b"]
    client.close()


def test_sync_client_close_does_not_close_running_loop_when_join_returns_early(monkeypatch):
    client = CodexAgenticClient()

    # Simulate a join timeout/early return: stop is requested, but we don't wait for the
    # loop thread to actually exit before proceeding.
    original_join = client._loop_thread.join

    def _fast_join(timeout: float | None = None) -> None:
        del timeout
        return

    monkeypatch.setattr(client._loop_thread, "join", _fast_join)
    try:
        client.close()
    finally:
        # Ensure the background loop thread is stopped/collected so the test doesn't leak threads.
        try:
            original_join(timeout=2.0)
        finally:
            if (not client._loop.is_running()) and (not client._loop.is_closed()):
                client._loop.close()


def test_sync_responses_events_closes_underlying_async_iterator_on_early_stop(monkeypatch):
    client = CodexAgenticClient()

    class _TrackedEventsIter:
        def __init__(self) -> None:
            self._step = 0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> ResponseEvent:
            if self._step == 0:
                self._step = 1
                return ResponseEvent(type="thread/ready", phase="system", session_id="thread-1")
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    tracked = _TrackedEventsIter()

    def _fake_events(**_: Any):
        return tracked

    monkeypatch.setattr(client._client, "responses_events", _fake_events)

    stream = client.responses_events(prompt="x")
    first = next(stream)
    assert first.type == "thread/ready"
    stream.close()
    assert tracked.closed is True
    client.close()


def test_sync_responses_stream_text_closes_underlying_async_iterator_on_early_stop(monkeypatch):
    client = CodexAgenticClient()

    class _TrackedTextIter:
        def __init__(self) -> None:
            self._step = 0
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            if self._step == 0:
                self._step = 1
                return "a"
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    tracked = _TrackedTextIter()

    def _fake_stream(**_: Any):
        return tracked

    monkeypatch.setattr(client._client, "responses_stream_text", _fake_stream)

    stream = client.responses_stream_text(prompt="x")
    first = next(stream)
    assert first == "a"
    stream.close()
    assert tracked.closed is True
    client.close()


def test_sync_client_context_entry_failure_closes_background_loop(monkeypatch):
    def _count_loop_threads() -> int:
        return sum(1 for thread in threading.enumerate() if thread.name == "codex-agentic-client-loop" and thread.is_alive())

    async def _fail_connect(self: AsyncCodexAgenticClient) -> None:
        del self
        raise RuntimeError("connect failed")

    before = _count_loop_threads()
    monkeypatch.setattr("codex_python_sdk.sync_client.AsyncCodexAgenticClient.connect", _fail_connect)

    try:
        with CodexAgenticClient():
            raise AssertionError("unreachable")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert str(exc) == "connect failed"

    after = _count_loop_threads()
    assert after == before


def test_connect_initialize_no_reentrant_deadlock(monkeypatch):
    class _FakeStdin:
        def __init__(self, stdout: asyncio.StreamReader):
            self._stdout = stdout
            self._closing = False
            self._loop = asyncio.get_running_loop()

        def is_closing(self) -> bool:
            return self._closing

        def close(self) -> None:
            self._closing = True

        def write(self, data: bytes) -> None:
            payload = json.loads(data.decode("utf-8").strip())
            if payload.get("method") == "initialize" and "id" in payload:
                response = {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"serverInfo": {"name": "codex-app-server"}},
                }
                line = (json.dumps(response) + "\n").encode("utf-8")
                self._loop.call_soon(self._stdout.feed_data, line)

        async def drain(self) -> None:
            return

    class _FakeProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()
            self.stdin = _FakeStdin(self.stdout)

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            return

        def kill(self) -> None:
            return

    async def _fake_create_subprocess_exec(*args: Any, **kwargs: Any):
        del args, kwargs
        return _FakeProcess()

    monkeypatch.setattr("codex_python_sdk.asyncio.create_subprocess_exec", _fake_create_subprocess_exec)

    async def _run():
        client = AsyncCodexAgenticClient()
        await asyncio.wait_for(client.connect(), timeout=1.0)
        assert client._connected is True
        await client.close()

    asyncio.run(_run())


def test_responses_events_raises_on_error_notification(monkeypatch):
    client = AsyncCodexAgenticClient()

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)

    async def _run():
        client._notification_queue.put_nowait({"method": "error", "params": {"message": "boom"}})
        events = []
        async for event in client.responses_events(prompt="x"):
            events.append(event)
        return events

    try:
        asyncio.run(_run())
        raise AssertionError("expected CodexAgenticError")
    except CodexAgenticError as exc:
        assert "boom" in str(exc)


def test_responses_events_raises_on_turn_failed_status(monkeypatch):
    client = AsyncCodexAgenticClient()

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    async def _fake_wait_for_matching_notification(thread_id: str, turn_id: str | None):
        assert thread_id == "thread-1"
        assert turn_id == "turn-1"
        return {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "tool failed"},
                },
            },
        }

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setattr(client, "_wait_for_matching_notification", _fake_wait_for_matching_notification)

    async def _run():
        events: list[ResponseEvent] = []
        async for event in client.responses_events(prompt="x"):
            events.append(event)
        return events

    try:
        asyncio.run(_run())
        raise AssertionError("expected CodexAgenticError")
    except CodexAgenticError as exc:
        assert "tool failed" in str(exc)


def test_async_client_close_drains_notification_queue():
    client = AsyncCodexAgenticClient()

    async def _run() -> None:
        client._notification_queue.put_nowait({"method": "turn/completed", "params": {"threadId": "t1"}})
        client._notification_queue.put_nowait({"method": "error", "params": {"message": "stale"}})
        client._notification_buffer.append({"method": "stale/buffered", "params": {}})
        await client.close()

    asyncio.run(_run())
    assert client._notification_queue.empty()
    assert client._notification_buffer == []


def test_async_client_close_clears_stderr_lines():
    client = AsyncCodexAgenticClient()

    async def _run() -> None:
        client._stderr_lines.extend(["line-1", "line-2"])
        await client.close()

    asyncio.run(_run())
    assert client._stderr_lines == []


def test_async_client_close_waits_after_kill(monkeypatch):
    client = AsyncCodexAgenticClient()

    class _FakeStdin:
        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            return

    class _FakeProcess:
        def __init__(self):
            self.stdin = _FakeStdin()
            self.terminate_called = False
            self.kill_called = False
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            return 0

        def terminate(self) -> None:
            self.terminate_called = True

        def kill(self) -> None:
            self.kill_called = True

    proc = _FakeProcess()
    client._proc = proc

    state = {"wait_for_calls": 0}
    original_wait_for = asyncio.wait_for

    async def _fake_wait_for(awaitable: Any, timeout: float):
        state["wait_for_calls"] += 1
        if state["wait_for_calls"] <= 2:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr("codex_python_sdk.async_client.asyncio.wait_for", _fake_wait_for)

    asyncio.run(client.close())
    assert proc.terminate_called is True
    assert proc.kill_called is True
    assert state["wait_for_calls"] == 3
    assert proc.wait_calls == 1


def test_responses_events_does_not_fail_on_idle_timeout_when_process_alive(monkeypatch):
    client = AsyncCodexAgenticClient()
    state = {"timed_out_once": False}

    class _FakeProcess:
        returncode = None

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    async def _fake_wait_for_matching_notification(thread_id: str, turn_id: str | None):
        assert thread_id == "thread-1"
        assert turn_id == "turn-1"
        if not state["timed_out_once"]:
            state["timed_out_once"] = True
            raise asyncio.TimeoutError
        return {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        }

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setattr(client, "_wait_for_matching_notification", _fake_wait_for_matching_notification)
    client._proc = _FakeProcess()
    client._connected = True

    async def _run():
        events = []
        async for event in client.responses_events(prompt="x"):
            events.append(event)
        return events

    events = asyncio.run(_run())
    assert [event.type for event in events] == ["thread/ready", "turn/completed"]
    assert state["timed_out_once"] is True


def test_responses_events_allows_repeated_idle_timeouts_before_idle_deadline(monkeypatch):
    client = AsyncCodexAgenticClient(stream_idle_timeout_seconds=60.0)
    state = {"timeouts": 0}

    class _FakeProcess:
        returncode = None

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    async def _fake_wait_for_matching_notification(thread_id: str, turn_id: str | None):
        assert thread_id == "thread-1"
        assert turn_id == "turn-1"
        if state["timeouts"] < 8:
            state["timeouts"] += 1
            raise asyncio.TimeoutError
        return {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        }

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setattr(client, "_wait_for_matching_notification", _fake_wait_for_matching_notification)
    client._proc = _FakeProcess()
    client._connected = True

    async def _run():
        events: list[ResponseEvent] = []
        async for event in client.responses_events(prompt="x"):
            events.append(event)
        return events

    events = asyncio.run(_run())
    assert [event.type for event in events] == ["thread/ready", "turn/completed"]
    assert state["timeouts"] == 8


def test_responses_events_uses_configurable_idle_timeout(monkeypatch):
    client = AsyncCodexAgenticClient(stream_idle_timeout_seconds=0.05)

    class _FakeProcess:
        returncode = None

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    async def _fake_wait_for_matching_notification(thread_id: str, turn_id: str | None):
        assert thread_id == "thread-1"
        assert turn_id == "turn-1"
        await asyncio.sleep(0.03)
        raise asyncio.TimeoutError

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setattr(client, "_wait_for_matching_notification", _fake_wait_for_matching_notification)
    client._proc = _FakeProcess()
    client._connected = True

    async def _run():
        async for _ in client.responses_events(prompt="x"):
            pass

    try:
        asyncio.run(_run())
        raise AssertionError("expected AppServerConnectionError")
    except AppServerConnectionError as exc:
        assert "Timed out waiting for matching notifications for 0.05 seconds." in str(exc)


def test_responses_events_raises_connection_error_when_idle_and_process_exited(monkeypatch):
    client = AsyncCodexAgenticClient()

    class _FakeProcess:
        returncode = 1

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    original_wait_for = asyncio.wait_for

    async def _fake_wait_for(awaitable: Any, timeout: float):
        if timeout == 5.0:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setattr("codex_python_sdk.async_client.asyncio.wait_for", _fake_wait_for)
    client._proc = _FakeProcess()

    async def _run():
        async for _ in client.responses_events(prompt="x"):
            pass

    try:
        asyncio.run(_run())
        raise AssertionError("expected AppServerConnectionError")
    except AppServerConnectionError as exc:
        assert "exited while waiting for notifications" in str(exc)


def test_responses_events_concurrent_streams_do_not_drop_notifications(monkeypatch):
    client = AsyncCodexAgenticClient()
    thread_start_count = 0
    turn_by_thread: dict[str, str] = {}

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        nonlocal thread_start_count
        if method == "thread/start":
            thread_start_count += 1
            return {"thread": {"id": f"thread-{thread_start_count}"}}
        if method == "turn/start":
            assert isinstance(params, dict)
            thread_id = str(params["threadId"])
            turn_id = f"turn-{thread_id.split('-')[-1]}"
            turn_by_thread[thread_id] = turn_id
            if len(turn_by_thread) == 2:
                second = "thread-2"
                first = "thread-1"
                client._notification_queue.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": second,
                            "turnId": turn_by_thread[second],
                            "turn": {"id": turn_by_thread[second], "status": "completed"},
                        },
                    }
                )
                client._notification_queue.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": first,
                            "turnId": turn_by_thread[first],
                            "turn": {"id": turn_by_thread[first], "status": "completed"},
                        },
                    }
                )
            return {"turn": {"id": turn_id}}
        raise AssertionError("unexpected method")

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)

    async def _collect(prompt: str) -> list[ResponseEvent]:
        out: list[ResponseEvent] = []
        async for event in client.responses_events(prompt=prompt):
            out.append(event)
        return out

    async def _run():
        return await asyncio.wait_for(
            asyncio.gather(
                _collect("first"),
                _collect("second"),
            ),
            timeout=2.0,
        )

    streams = asyncio.run(_run())
    stream_event_types = [[event.type for event in stream] for stream in streams]
    assert stream_event_types == [
        ["thread/ready", "turn/completed"],
        ["thread/ready", "turn/completed"],
    ]


def test_notification_router_bounds_buffer_size():
    client = AsyncCodexAgenticClient()
    client._notification_buffer_limit = 3

    async def _run() -> list[str]:
        task = asyncio.create_task(client._notification_router_loop())
        try:
            for idx in range(5):
                client._notification_queue.put_nowait({"method": f"m{idx}", "params": {"threadId": "t1"}})

            for _ in range(50):
                if len(client._notification_buffer) >= 3:
                    break
                await asyncio.sleep(0.01)

            methods = [str(item.get("method")) for item in client._notification_buffer]
            return methods
        finally:
            task.cancel()
            await task

    methods = asyncio.run(_run())
    assert methods == ["m2", "m3", "m4"]
    assert len(methods) == 3


def test_stderr_loop_bounds_buffer_size():
    client = AsyncCodexAgenticClient()
    client._stderr_buffer_limit = 3

    class _FakeProcess:
        def __init__(self, stderr: asyncio.StreamReader) -> None:
            self.stderr = stderr

    async def _run() -> list[str]:
        stderr = asyncio.StreamReader()
        for idx in range(5):
            stderr.feed_data(f"line-{idx}\n".encode("utf-8"))
        stderr.feed_eof()
        client._proc = _FakeProcess(stderr)
        await client._stderr_loop()
        return client._stderr_lines

    lines = asyncio.run(_run())
    assert lines == ["line-2", "line-3", "line-4"]


def test_selected_async_methods_build_expected_requests(monkeypatch):
    client = AsyncCodexAgenticClient(
        default_thread_params={"approvalPolicy": "on-request", "sandbox": "workspace-write"},
    )
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def _fake_request(method: str, params: dict[str, Any] | None, **kwargs: Any):
        assert kwargs in ({}, {"timeout_seconds": 31.2})
        calls.append((method, params))
        return {"ok": True}

    monkeypatch.setattr(client, "_request", _fake_request)

    async def _run():
        await client.thread_fork(
            "thread-1",
            params={
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "model": "model-1",
                "cwd": "/tmp/work",
                "baseInstructions": "base",
                "developerInstructions": "dev",
                "path": "/tmp/path",
                "modelProvider": "openai",
                "config": {"explicit": 1},
            },
        )
        await client.thread_name_set("thread-1", "new-name")
        await client.thread_unarchive("thread-1")
        await client.thread_compact_start("thread-1")
        await client.thread_rollback("thread-1", 2)
        await client.thread_loaded_list(limit=10, cursor="cur-1")
        await client.skills_list(cwds=["/tmp/a", "/tmp/b"], force_reload=True)
        await client.skills_remote_read()
        await client.skills_remote_write("hz-1", True)
        await client.app_list(limit=5, cursor="cur-2")
        await client.skills_config_write("/tmp/skill", True)
        await client.turn_interrupt("thread-1", "turn-1")
        await client.review_start("thread-1", {"type": "uncommittedChanges"}, delivery="detached")
        await client.model_list(limit=3, cursor="cur-3")
        await client.account_rate_limits_read()
        await client.account_read(refresh_token=True)

    asyncio.run(_run())

    methods = [method for method, _ in calls]
    assert methods == [
        "thread/fork",
        "thread/name/set",
        "thread/unarchive",
        "thread/compact/start",
        "thread/rollback",
        "thread/loaded/list",
        "skills/list",
        "skills/remote/read",
        "skills/remote/write",
        "app/list",
        "skills/config/write",
        "turn/interrupt",
        "review/start",
        "model/list",
        "account/rateLimits/read",
        "account/read",
    ]

    fork_params = calls[0][1]
    assert isinstance(fork_params, dict)
    assert fork_params["threadId"] == "thread-1"
    assert fork_params["approvalPolicy"] == "never"
    assert fork_params["sandbox"] == "danger-full-access"
    assert fork_params["model"] == "model-1"
    assert fork_params["cwd"] == "/tmp/work"
    assert fork_params["baseInstructions"] == "base"
    assert fork_params["developerInstructions"] == "dev"
    assert fork_params["path"] == "/tmp/path"
    assert fork_params["modelProvider"] == "openai"
    assert fork_params["config"] == {"explicit": 1}

    assert calls[5][1] == {"limit": 10, "cursor": "cur-1"}
    assert calls[6][1] == {"cwds": ["/tmp/a", "/tmp/b"], "forceReload": True}
    assert calls[7][1] == {}
    assert calls[8][1] == {"hazelnutId": "hz-1", "isPreload": True}
    assert calls[11][1] == {"threadId": "thread-1", "turnId": "turn-1"}
    assert calls[12][1] == {"threadId": "thread-1", "target": {"type": "uncommittedChanges"}, "delivery": "detached"}
    assert calls[13][1] == {"limit": 3, "cursor": "cur-3"}
    assert calls[14][1] is None
    assert calls[15][1] == {"refreshToken": True}


def test_new_async_methods_build_expected_requests(monkeypatch):
    client = AsyncCodexAgenticClient()
    calls: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] = []

    async def _fake_request(method: str, params: dict[str, Any] | None, **kwargs: Any):
        calls.append((method, params, kwargs))
        return {"ok": True}

    monkeypatch.setattr(client, "_request", _fake_request)

    async def _run():
        await client.turn_steer("thread-1", "turn-1", "keep going")
        await client.command_exec(
            ["bash", "-lc", "pwd"],
            cwd="/tmp/work",
            timeout_ms=1200,
            sandbox_policy={"type": "readOnly"},
        )
        await client.config_read(cwd="/tmp/work", include_layers=True)
        await client.config_value_write(
            "tools.web_search",
            True,
            merge_strategy="replace",
            file_path="/tmp/config.toml",
            expected_version="v1",
        )
        await client.config_batch_write(
            [{"keyPath": "model", "mergeStrategy": "replace", "value": "gpt-5"}],
            file_path="/tmp/config.toml",
            expected_version="v2",
        )
        await client.config_requirements_read()
        await client.config_mcp_server_reload()
        await client.mcp_server_status_list(limit=7, cursor="cur-1")
        await client.mcp_server_oauth_login("github", scopes=["repo"], timeout_secs=30)
        await client.fuzzy_file_search("readme", roots=["/tmp/work"], cancellation_token="tok-1")

    asyncio.run(_run())

    methods = [method for method, _, _ in calls]
    assert methods == [
        "turn/steer",
        "command/exec",
        "config/read",
        "config/value/write",
        "config/batchWrite",
        "configRequirements/read",
        "config/mcpServer/reload",
        "mcpServerStatus/list",
        "mcpServer/oauth/login",
        "fuzzyFileSearch",
    ]
    assert calls[0][1] == {
        "threadId": "thread-1",
        "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "keep going", "text_elements": []}],
    }
    assert calls[0][2] == {}
    assert calls[1][1] == {
        "command": ["bash", "-lc", "pwd"],
        "cwd": "/tmp/work",
        "timeoutMs": 1200,
        "sandboxPolicy": {"type": "readOnly"},
    }
    assert abs(calls[1][2]["timeout_seconds"] - 31.2) < 1e-9
    assert calls[2][1] == {"cwd": "/tmp/work", "includeLayers": True}
    assert calls[2][2] == {}
    assert calls[3][1] == {
        "keyPath": "tools.web_search",
        "value": True,
        "mergeStrategy": "replace",
        "filePath": "/tmp/config.toml",
        "expectedVersion": "v1",
    }
    assert calls[3][2] == {}
    assert calls[4][1] == {
        "edits": [{"keyPath": "model", "mergeStrategy": "replace", "value": "gpt-5"}],
        "filePath": "/tmp/config.toml",
        "expectedVersion": "v2",
    }
    assert calls[4][2] == {}
    assert calls[5][1] is None
    assert calls[5][2] == {}
    assert calls[6][1] is None
    assert calls[6][2] == {}
    assert calls[7][1] == {"limit": 7, "cursor": "cur-1"}
    assert calls[7][2] == {}
    assert calls[8][1] == {"name": "github", "scopes": ["repo"], "timeoutSecs": 30}
    assert calls[8][2] == {}
    assert calls[9][1] == {"query": "readme", "roots": ["/tmp/work"], "cancellationToken": "tok-1"}
    assert calls[9][2] == {}


def test_fuzzy_file_search_uses_process_cwd_by_default(monkeypatch):
    client = AsyncCodexAgenticClient(process_cwd="/tmp/base")
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def _fake_request(method: str, params: dict[str, Any] | None):
        calls.append((method, params))
        return {"ok": True}

    monkeypatch.setattr(client, "_request", _fake_request)

    asyncio.run(client.fuzzy_file_search("main.py"))
    assert calls == [("fuzzyFileSearch", {"query": "main.py", "roots": ["/tmp/base"]})]


def test_sync_selected_methods_delegate_to_async(monkeypatch):
    client = CodexAgenticClient()

    async def _fake_thread_name_set(thread_id: str, name: str):
        return {"thread_id": thread_id, "name": name}

    async def _fake_turn_interrupt(thread_id: str, turn_id: str):
        return {"thread_id": thread_id, "turn_id": turn_id}

    async def _fake_account_read(*, refresh_token: bool | None = None):
        return {"refresh_token": refresh_token}

    monkeypatch.setattr(client._client, "thread_name_set", _fake_thread_name_set)
    monkeypatch.setattr(client._client, "turn_interrupt", _fake_turn_interrupt)
    monkeypatch.setattr(client._client, "account_read", _fake_account_read)

    assert client.thread_name_set("t1", "n1") == {"thread_id": "t1", "name": "n1"}
    assert client.turn_interrupt("t1", "u1") == {"thread_id": "t1", "turn_id": "u1"}
    assert client.account_read(refresh_token=True) == {"refresh_token": True}
    client.close()


def test_sync_new_methods_delegate_to_async(monkeypatch):
    client = CodexAgenticClient()

    async def _fake_turn_steer(thread_id: str, turn_id: str, prompt: str):
        return {"thread_id": thread_id, "turn_id": turn_id, "prompt": prompt}

    async def _fake_config_read(*, cwd: str | None = None, include_layers: bool = False):
        return {"cwd": cwd, "include_layers": include_layers}

    async def _fake_command_exec(
        command: list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
        sandbox_policy: dict[str, Any] | None = None,
    ):
        return {
            "command": command,
            "cwd": cwd,
            "timeout_ms": timeout_ms,
            "sandbox_policy": sandbox_policy,
        }

    monkeypatch.setattr(client._client, "turn_steer", _fake_turn_steer)
    monkeypatch.setattr(client._client, "config_read", _fake_config_read)
    monkeypatch.setattr(client._client, "command_exec", _fake_command_exec)

    assert client.turn_steer("t1", "u1", "continue") == {"thread_id": "t1", "turn_id": "u1", "prompt": "continue"}
    assert client.config_read(cwd="/tmp/work", include_layers=True) == {"cwd": "/tmp/work", "include_layers": True}
    assert client.command_exec(
        ["pwd"],
        cwd="/tmp/work",
        timeout_ms=50,
        sandbox_policy={"type": "readOnly"},
    ) == {
        "command": ["pwd"],
        "cwd": "/tmp/work",
        "timeout_ms": 50,
        "sandbox_policy": {"type": "readOnly"},
    }
    client.close()


def test_sync_client_works_inside_running_event_loop(monkeypatch):
    client = CodexAgenticClient()

    async def _fake_account_read(*, refresh_token: bool | None = None):
        return {"refresh_token": refresh_token}

    monkeypatch.setattr(client._client, "account_read", _fake_account_read)

    async def _run():
        return client.account_read(refresh_token=True)

    result = asyncio.run(_run())
    assert result == {"refresh_token": True}
    client.close()


def test_responses_events_applies_thread_and_turn_params(monkeypatch):
    client = AsyncCodexAgenticClient(
        default_thread_params={"sandbox": "workspace-write"},
        default_turn_params={"effort": "medium"},
    )
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            client._notification_queue.put_nowait(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
                }
            )
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)

    async def _run():
        out: list[ResponseEvent] = []
        async for event in client.responses_events(
            prompt="hello",
            thread_params={"model": "gpt-5"},
            turn_params={"effort": "high", "outputSchema": {"type": "object"}},
        ):
            out.append(event)
        return out

    events = asyncio.run(_run())
    assert len(events) == 2
    assert events[0].type == "thread/ready"
    assert events[1].type == "turn/completed"
    assert calls[0] == ("thread/start", {"sandbox": "workspace-write", "model": "gpt-5"})
    assert calls[1][0] == "turn/start"
    assert calls[1][1]["threadId"] == "thread-1"
    assert calls[1][1]["effort"] == "high"
    assert calls[1][1]["outputSchema"] == {"type": "object"}


def test_responses_events_prevents_overrides_of_thread_id_and_input(monkeypatch):
    client = AsyncCodexAgenticClient(
        default_thread_params={"sandbox": "workspace-write"},
        default_turn_params={"effort": "medium"},
    )
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        calls.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            client._notification_queue.put_nowait(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
                }
            )
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)

    async def _run():
        async for _ in client.responses_events(
            prompt="authoritative prompt",
            session_id="thread-1",
            thread_params={"threadId": "thread-evil", "model": "gpt-5"},
            turn_params={
                "threadId": "thread-evil",
                "input": [{"type": "text", "text": "evil prompt", "text_elements": []}],
                "effort": "high",
            },
        ):
            pass

    asyncio.run(_run())
    assert calls[0][0] == "thread/resume"
    assert calls[0][1] == {"sandbox": "workspace-write", "threadId": "thread-1", "model": "gpt-5"}
    assert calls[1][0] == "turn/start"
    assert calls[1][1]["threadId"] == "thread-1"
    assert calls[1][1]["input"] == [{"type": "text", "text": "authoritative prompt", "text_elements": []}]
    assert calls[1][1]["effort"] == "high"


def test_responses_events_falls_back_when_resume_has_no_rollout(monkeypatch):
    client = AsyncCodexAgenticClient()
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def _fake_connect():
        client._connected = True

    async def _fake_request(method: str, params: dict[str, Any] | None):
        calls.append((method, params))
        if method == "thread/resume":
            raise CodexAgenticError("no rollout found for thread id thread-1")
        if method == "turn/start":
            client._notification_queue.put_nowait(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
                }
            )
            return {"turn": {"id": "turn-1"}}
        raise AssertionError("unexpected method")

    monkeypatch.setattr(client, "connect", _fake_connect)
    monkeypatch.setattr(client, "_request", _fake_request)

    async def _run():
        out: list[ResponseEvent] = []
        async for event in client.responses_events(prompt="hello", session_id="thread-1"):
            out.append(event)
        return out

    events = asyncio.run(_run())
    assert [event.type for event in events] == ["thread/ready", "turn/completed"]
    assert events[0].session_id == "thread-1"
    assert calls[0][0] == "thread/resume"
    assert calls[1][0] == "turn/start"
    assert calls[1][1]["threadId"] == "thread-1"


def test_sync_thread_start_passes_params(monkeypatch):
    client = CodexAgenticClient()

    async def _fake_thread_start(*, params: dict[str, Any] | None = None):
        return {"thread": {"id": f"thread:{params.get('model') if params else 'none'}"}}

    monkeypatch.setattr(client._client, "thread_start", _fake_thread_start)
    out = client.thread_start(params={"model": "gpt-5"})
    assert out == {"thread": {"id": "thread:gpt-5"}}
    client.close()


def test_server_request_handlers_can_override_default_decisions(monkeypatch):
    async def _command_handler(params: dict[str, Any]) -> dict[str, Any]:
        return {"decision": "deny", "reason": f"blocked:{params.get('command')}"}

    def _file_handler(params: dict[str, Any]) -> dict[str, Any]:
        return {"decision": "reject", "reason": params.get("reason", "manual")}

    def _user_input_handler(params: dict[str, Any]) -> dict[str, Any]:
        request_id = params.get("requestId")
        return {"answers": {"request_id": request_id}}

    def _tool_call_handler(params: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "contentItems": [{"type": "inputText", "text": f"tool:{params.get('tool')}"}],
        }

    client = AsyncCodexAgenticClient(
        on_command_approval=_command_handler,
        on_file_change_approval=_file_handler,
        on_tool_request_user_input=_user_input_handler,
        on_tool_call=_tool_call_handler,
    )
    sent: list[dict[str, Any]] = []

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "rm -rf /"},
            }
        )
        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "item/fileChange/requestApproval",
                "params": {"reason": "policy"},
            }
        )
        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "item/tool/requestUserInput",
                "params": {"requestId": "r-1"},
            }
        )
        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "item/tool/call",
                "params": {"tool": "web.search"},
            }
        )

    asyncio.run(_run())
    assert sent[0]["result"] == {"decision": "deny", "reason": "blocked:rm -rf /"}
    assert sent[1]["result"] == {"decision": "reject", "reason": "policy"}
    assert sent[2]["result"] == {"answers": {"request_id": "r-1"}}
    assert sent[3]["result"] == {
        "success": True,
        "contentItems": [{"type": "inputText", "text": "tool:web.search"}],
    }


def test_tool_request_user_input_alias_is_rejected(monkeypatch):
    def _user_input_handler(params: dict[str, Any]) -> dict[str, Any]:
        return {"answers": {"request_id": params.get("requestId")}}

    client = AsyncCodexAgenticClient(on_tool_request_user_input=_user_input_handler)
    sent: list[dict[str, Any]] = []

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tool/requestUserInput",
                "params": {"requestId": "r-2"},
            }
        )

    asyncio.run(_run())
    assert sent[0]["error"]["code"] == -32601
    assert "Unsupported server request method: tool/requestUserInput" == sent[0]["error"]["message"]


def test_tool_request_user_input_uses_policy_engine_when_hook_missing(monkeypatch):
    class _PolicyEngine:
        def on_tool_request_user_input(self, params: dict[str, Any], context: Any) -> dict[str, Any]:
            return {
                "answers": {
                    "request_id": params.get("requestId"),
                    "method": context.method,
                }
            }

    client = AsyncCodexAgenticClient(policy_engine=_PolicyEngine())
    sent: list[dict[str, Any]] = []

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "item/tool/requestUserInput",
                "params": {"requestId": "r-3"},
            }
        )

    asyncio.run(_run())
    assert sent[0]["result"] == {"answers": {"request_id": "r-3", "method": "item/tool/requestUserInput"}}


def test_tool_call_server_request_uses_default_failure_without_handler(monkeypatch):
    client = AsyncCodexAgenticClient()
    sent: list[dict[str, Any]] = []

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._handle_server_request(
            {"jsonrpc": "2.0", "id": 12, "method": "item/tool/call", "params": {"tool": "dynamic"}}
        )

    asyncio.run(_run())
    assert sent[0]["result"] == {
        "success": False,
        "contentItems": [{"type": "inputText", "text": "No tool-call handler configured."}],
    }


def test_server_request_dispatch_sends_error_on_handler_exception(monkeypatch):
    client = AsyncCodexAgenticClient()
    sent: list[dict[str, Any]] = []

    async def _fake_handle_server_request(msg: dict[str, Any]) -> None:
        del msg
        raise RuntimeError("handler exploded")

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_handle_server_request", _fake_handle_server_request)
    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._dispatch_server_request(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "pwd"},
            }
        )

    asyncio.run(_run())
    assert sent[0]["id"] == 11
    assert sent[0]["error"]["code"] == -32603
    assert "Server request handler failed." == sent[0]["error"]["message"]


def test_server_request_dispatch_sends_error_for_string_request_id(monkeypatch):
    client = AsyncCodexAgenticClient()
    sent: list[dict[str, Any]] = []

    async def _fake_handle_server_request(msg: dict[str, Any]) -> None:
        del msg
        raise RuntimeError("handler exploded")

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_handle_server_request", _fake_handle_server_request)
    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._dispatch_server_request(
            {
                "jsonrpc": "2.0",
                "id": "req-err-1",
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "pwd"},
            }
        )

    asyncio.run(_run())
    assert sent[0]["id"] == "req-err-1"
    assert sent[0]["error"]["code"] == -32603
    assert "Server request handler failed." == sent[0]["error"]["message"]


def test_server_request_handler_accepts_string_request_id(monkeypatch):
    async def _command_handler(params: dict[str, Any]) -> dict[str, Any]:
        return {"decision": "accept", "reason": f"allowed:{params.get('command')}"}

    client = AsyncCodexAgenticClient(on_command_approval=_command_handler)
    sent: list[dict[str, Any]] = []

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": "req-11",
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "pwd"},
            }
        )

    asyncio.run(_run())
    assert sent[0]["id"] == "req-11"
    assert sent[0]["result"] == {"decision": "accept", "reason": "allowed:pwd"}


def test_server_request_handler_ignores_bool_request_id(monkeypatch):
    client = AsyncCodexAgenticClient()
    sent: list[dict[str, Any]] = []

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": True,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "pwd"},
            }
        )

    asyncio.run(_run())
    assert sent == []


def test_server_request_dispatch_ignores_bool_request_id_on_handler_error(monkeypatch):
    client = AsyncCodexAgenticClient()
    sent: list[dict[str, Any]] = []

    async def _fake_handle_server_request(msg: dict[str, Any]) -> None:
        del msg
        raise RuntimeError("handler exploded")

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_handle_server_request", _fake_handle_server_request)
    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._dispatch_server_request(
            {
                "jsonrpc": "2.0",
                "id": False,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "pwd"},
            }
        )

    asyncio.run(_run())
    assert sent == []


def test_server_request_dispatch_preserves_cancellation(monkeypatch):
    client = AsyncCodexAgenticClient()
    sent: list[dict[str, Any]] = []

    async def _fake_handle_server_request(msg: dict[str, Any]) -> None:
        del msg
        raise asyncio.CancelledError

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_handle_server_request", _fake_handle_server_request)
    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        try:
            await client._dispatch_server_request(
                {
                    "jsonrpc": "2.0",
                    "id": "cancel-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "pwd"},
                }
            )
            raise AssertionError("expected CancelledError")
        except asyncio.CancelledError:
            return

    asyncio.run(_run())
    assert sent == []


def test_legacy_server_request_methods_are_rejected(monkeypatch):
    client = AsyncCodexAgenticClient()
    sent: list[dict[str, Any]] = []

    async def _fake_send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(client, "_send_json", _fake_send_json)

    async def _run():
        await client._handle_server_request({"jsonrpc": "2.0", "id": 9, "method": "applyPatchApproval", "params": {}})
        await client._handle_server_request({"jsonrpc": "2.0", "id": 10, "method": "execCommandApproval", "params": {}})

    asyncio.run(_run())
    assert sent[0]["error"]["code"] == -32601
    assert "Unsupported server request method: applyPatchApproval" in sent[0]["error"]["message"]
    assert sent[1]["error"]["code"] == -32601
    assert "Unsupported server request method: execCommandApproval" in sent[1]["error"]["message"]


def test_notification_to_event_extracts_selected_new_notifications():
    name_payload = {
        "method": "thread/name/updated",
        "params": {"threadId": "t1", "threadName": "renamed"},
    }
    usage_payload = {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "t1",
            "turnId": "u1",
            "tokenUsage": {
                "last": {"totalTokens": 12},
                "total": {"totalTokens": 34},
            },
        },
    }
    diff_payload = {
        "method": "turn/diff/updated",
        "params": {
            "threadId": "t1",
            "turnId": "u1",
            "diff": "--- a/x\n+++ b/x\n+new\n-old\n",
        },
    }
    plan_payload = {
        "method": "turn/plan/updated",
        "params": {
            "threadId": "t1",
            "turnId": "u1",
            "explanation": "doing work",
            "plan": [
                {"status": "inProgress", "step": "a"},
                {"status": "pending", "step": "b"},
                {"status": "completed", "step": "c"},
            ],
        },
    }
    summary_part_payload = {
        "method": "item/reasoning/summaryPartAdded",
        "params": {"threadId": "t1", "turnId": "u1", "itemId": "it1", "summaryIndex": 2},
    }
    compacted_payload = {
        "method": "thread/compacted",
        "params": {"threadId": "t1", "turnId": "u1"},
    }

    e1 = AsyncCodexAgenticClient._notification_to_event(name_payload)
    e2 = AsyncCodexAgenticClient._notification_to_event(usage_payload)
    e3 = AsyncCodexAgenticClient._notification_to_event(diff_payload)
    e4 = AsyncCodexAgenticClient._notification_to_event(plan_payload)
    e5 = AsyncCodexAgenticClient._notification_to_event(summary_part_payload)
    e6 = AsyncCodexAgenticClient._notification_to_event(compacted_payload)

    assert e1.thread_name == "renamed"
    assert e2.turn_id == "u1"
    assert e2.token_usage == usage_payload["params"]["tokenUsage"]
    assert e2.message_text == "last=12, total=34"
    assert e3.diff == diff_payload["params"]["diff"]
    assert e3.message_text == "+1 -1"
    assert e4.plan is not None and len(e4.plan) == 3
    assert "steps=3" in (e4.message_text or "")
    assert e5.phase == "planning"
    assert e5.item_id == "it1"
    assert e5.summary_index == 2
    assert e6.message_text == "thread compacted"


def test_render_exec_style_events_formats_selected_new_notifications():
    sink = StringIO()
    events = iter(
        [
            ResponseEvent(
                type="thread/name/updated",
                phase="system",
                thread_name="renamed",
                message_text="thread name: renamed",
                raw={"params": {"threadName": "renamed"}},
            ),
            ResponseEvent(
                type="thread/tokenUsage/updated",
                phase="system",
                message_text="last=12, total=34",
                token_usage={"last": {"totalTokens": 12}, "total": {"totalTokens": 34}},
                raw={"params": {}},
            ),
            ResponseEvent(
                type="turn/diff/updated",
                phase="system",
                message_text="+2 -1",
                diff="+a\n+b\n-c",
                raw={"params": {}},
            ),
            ResponseEvent(
                type="turn/plan/updated",
                phase="planning",
                message_text="steps=2, inProgress=1, pending=1",
                plan=[{"status": "inProgress", "step": "a"}, {"status": "pending", "step": "b"}],
                raw={"params": {}},
            ),
            ResponseEvent(
                type="item/reasoning/summaryPartAdded",
                phase="planning",
                summary_index=2,
                raw={"params": {}},
            ),
            ResponseEvent(type="thread/compacted", phase="system", raw={"params": {}}),
        ]
    )

    render_exec_style_events(events, stream=sink, show_reasoning=True, show_system=True, color="off")
    out = sink.getvalue()
    assert "[thread.name] renamed" in out
    assert "[usage] last=12, total=34" in out
    assert "[turn.diff] +2 -1" in out
    assert "[turn.plan] steps=2, inProgress=1, pending=1" in out
    assert "[thinking] summary part #2" in out
    assert "[context] compacted" in out


def test_render_exec_style_events_hides_selected_system_notifications_when_disabled():
    sink = StringIO()
    events = iter(
        [
            ResponseEvent(type="turn/diff/updated", phase="system", message_text="+1 -0", raw={"params": {}}),
            ResponseEvent(type="thread/name/updated", phase="system", thread_name="renamed", raw={"params": {}}),
        ]
    )

    render_exec_style_events(events, stream=sink, show_system=False, color="off")
    assert sink.getvalue() == ""
