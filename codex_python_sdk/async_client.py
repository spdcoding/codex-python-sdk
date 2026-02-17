from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from ._shared import (
    diff_change_counts,
    first_nonempty_text,
    token_usage_summary,
    turn_plan_summary,
)
from .errors import (
    AppServerConnectionError,
    CodexAgenticError,
    NotAuthenticatedError,
    SessionNotFoundError,
)
from .types import AgentResponse, ResponseEvent

if TYPE_CHECKING:
    from .policy import PolicyContext, PolicyEngine, PolicyJudgeConfig, PolicyRubric

DEFAULT_CLI_COMMAND = "codex"
DEFAULT_APP_SERVER_ARGS = ["app-server"]
DEFAULT_NOTIFICATION_BUFFER_LIMIT = 1024
DEFAULT_STDERR_BUFFER_LIMIT = 500
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 60.0


class AsyncCodexAgenticClient:
    """Async wrapper around `codex app-server` using JSON-RPC over stdio."""

    def __init__(
        self,
        *,
        codex_command: str = DEFAULT_CLI_COMMAND,
        app_server_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        process_cwd: str | None = None,
        default_thread_params: dict[str, Any] | None = None,
        default_turn_params: dict[str, Any] | None = None,
        enable_web_search: bool = True,
        server_config_overrides: dict[str, Any] | None = None,
        stream_idle_timeout_seconds: float | None = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        on_command_approval: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        on_file_change_approval: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        on_tool_request_user_input: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        on_tool_call: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        policy_engine: "PolicyEngine | None" = None,
        policy_rubric: "PolicyRubric | dict[str, Any] | None" = None,
        policy_judge_config: "PolicyJudgeConfig | None" = None,
    ) -> None:
        """Create an async app-server client.

        Args:
            codex_command: Executable name/path, usually ``"codex"``.
            app_server_args: CLI args passed after ``codex``; defaults to ``["app-server"]``.
            env: Environment for the child process.
            process_cwd: Working directory used when launching app-server.
            default_thread_params: Baseline params for thread-level requests.
            default_turn_params: Baseline params for turn-level requests.
            enable_web_search: If true, appends ``--enable web_search`` at launch.
            server_config_overrides: Config key-values serialized to ``-c key=value``.
            stream_idle_timeout_seconds: Max consecutive seconds without matching turn events
                before stream wait fails. Set ``None`` to disable this guard.
            on_command_approval: Handler for ``item/commandExecution/requestApproval``.
            on_file_change_approval: Handler for ``item/fileChange/requestApproval``.
            on_tool_request_user_input: Handler for ``item/tool/requestUserInput``.
            on_tool_call: Handler for ``item/tool/call``.
            policy_engine: Optional policy engine used when explicit hooks are absent.
            policy_rubric: Optional rubric used to auto-build a policy engine when
                ``policy_engine`` is not provided.
            policy_judge_config: Optional LLM-judge settings when rubric builds an LLM policy.
        """

        self.codex_command = codex_command
        self.app_server_args = app_server_args[:] if app_server_args else DEFAULT_APP_SERVER_ARGS[:]
        self.env = os.environ.copy() if env is None else env.copy()

        self.process_cwd = os.path.abspath(process_cwd or os.getcwd())
        self.default_thread_params = dict(default_thread_params or {})
        self.default_turn_params = dict(default_turn_params or {})
        self.enable_web_search = enable_web_search
        self.server_config_overrides = dict(server_config_overrides or {})
        self.stream_idle_timeout_seconds = stream_idle_timeout_seconds
        self.on_command_approval = on_command_approval
        self.on_file_change_approval = on_file_change_approval
        self.on_tool_request_user_input = on_tool_request_user_input
        self.on_tool_call = on_tool_call
        self.policy_engine = policy_engine
        if self.policy_engine is None and policy_rubric is not None:
            from .policy import build_policy_engine_from_rubric

            self.policy_engine = build_policy_engine_from_rubric(
                policy_rubric,
                judge_config=policy_judge_config,
                codex_command=codex_command,
                app_server_args=app_server_args,
                env=env,
                process_cwd=self.process_cwd,
                server_config_overrides=server_config_overrides,
            )

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._connected = False
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._connect_lock_ref: asyncio.Lock | None = None
        self._write_lock_ref: asyncio.Lock | None = None
        self._notification_queue_ref: asyncio.Queue[dict[str, Any]] | None = None
        self._notification_buffer: list[dict[str, Any]] = []
        self._notification_buffer_limit = DEFAULT_NOTIFICATION_BUFFER_LIMIT
        self._notification_condition_ref: asyncio.Condition | None = None
        self._notification_router_task: asyncio.Task[None] | None = None
        self._stderr_buffer_limit = DEFAULT_STDERR_BUFFER_LIMIT
        self._stderr_lines: list[str] = []
        self._user_agent: str | None = None

    async def __aenter__(self) -> "AsyncCodexAgenticClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    def _ensure_runtime_primitives(self) -> None:
        loop = asyncio.get_running_loop()
        if self._runtime_loop is None:
            self._runtime_loop = loop
        elif self._runtime_loop is not loop:
            raise CodexAgenticError("Async client cannot be shared across different event loops.")

        if self._connect_lock_ref is None:
            self._connect_lock_ref = asyncio.Lock()
        if self._write_lock_ref is None:
            self._write_lock_ref = asyncio.Lock()
        if self._notification_queue_ref is None:
            self._notification_queue_ref = asyncio.Queue()
        if self._notification_condition_ref is None:
            self._notification_condition_ref = asyncio.Condition()

    @property
    def _connect_lock(self) -> asyncio.Lock:
        if self._connect_lock_ref is None:
            self._ensure_runtime_primitives()
        assert self._connect_lock_ref is not None
        return self._connect_lock_ref

    @property
    def _write_lock(self) -> asyncio.Lock:
        if self._write_lock_ref is None:
            self._ensure_runtime_primitives()
        assert self._write_lock_ref is not None
        return self._write_lock_ref

    @property
    def _notification_queue(self) -> asyncio.Queue[dict[str, Any]]:
        if self._notification_queue_ref is None:
            self._ensure_runtime_primitives()
        assert self._notification_queue_ref is not None
        return self._notification_queue_ref

    @property
    def _notification_condition(self) -> asyncio.Condition:
        if self._notification_condition_ref is None:
            self._ensure_runtime_primitives()
        assert self._notification_condition_ref is not None
        return self._notification_condition_ref

    async def connect(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    self.codex_command,
                    *self._build_server_args(),
                    cwd=self.process_cwd,
                    env=self.env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:
                self._raise_connection_error(exc)

            assert self._proc is not None
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())
            self._ensure_notification_router()

            try:
                init_result = await self._request(
                    "initialize",
                    {
                        "clientInfo": {"name": "codex-python-sdk", "version": "0.1"},
                        "capabilities": {"experimentalApi": True},
                    },
                    ensure_connected=False,
                )
                await self._notify("initialized", None)
                self._user_agent = first_nonempty_text(init_result)
                self._connected = True
            except Exception:
                await self.close()
                raise

    async def close(self) -> None:
        self._connected = False
        await self._close_policy_engine()

        if self._proc is not None:
            proc = self._proc
            self._proc = None
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except Exception:
                        pass

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AppServerConnectionError("App-server transport closed."))
        self._pending.clear()

        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        if self._notification_router_task is not None:
            self._notification_router_task.cancel()
        self._notification_router_task = None
        while True:
            try:
                self._notification_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        async with self._notification_condition:
            self._notification_buffer.clear()
            self._notification_condition.notify_all()
        self._stderr_lines.clear()

    async def _close_policy_engine(self) -> None:
        engine = self.policy_engine
        if engine is None:
            return

        for close_name in ("aclose", "close"):
            closer = getattr(engine, close_name, None)
            if not callable(closer):
                continue
            maybe_result = closer()
            if inspect.isawaitable(maybe_result):
                await maybe_result
            return

    def _build_server_args(self) -> list[str]:
        args = self.app_server_args[:]
        if self.enable_web_search:
            args.extend(["--enable", "web_search"])
        for key, value in self.server_config_overrides.items():
            args.extend(["-c", f"{key}={self._to_toml_literal(value)}"])
        return args

    @staticmethod
    def _to_toml_literal(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(AsyncCodexAgenticClient._to_toml_literal(v) for v in value) + "]"
        if isinstance(value, dict):
            fields: list[str] = []
            for key, nested in value.items():
                quoted_key = json.dumps(str(key))
                fields.append(f"{quoted_key} = {AsyncCodexAgenticClient._to_toml_literal(nested)}")
            return "{" + ", ".join(fields) + "}"
        return json.dumps(str(value))

    async def _reader_loop(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue

                if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                    msg_id = msg.get("id")
                    if isinstance(msg_id, int) and msg_id in self._pending:
                        fut = self._pending.pop(msg_id)
                        if not fut.done():
                            fut.set_result(msg)
                    continue

                if isinstance(msg, dict) and "method" in msg and "id" in msg:
                    asyncio.create_task(self._dispatch_server_request(msg))
                    continue

                if isinstance(msg, dict) and "method" in msg:
                    await self._notification_queue.put(msg)
        finally:
            self._connected = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AppServerConnectionError("App-server stream ended unexpectedly."))
            self._pending.clear()
            async with self._notification_condition:
                self._notification_condition.notify_all()

    async def _stderr_loop(self) -> None:
        assert self._proc is not None
        assert self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_lines.append(text)
                overflow = len(self._stderr_lines) - self._stderr_buffer_limit
                if overflow > 0:
                    del self._stderr_lines[:overflow]

    def _ensure_notification_router(self) -> None:
        task = self._notification_router_task
        if task is not None and not task.done():
            return
        self._notification_router_task = asyncio.create_task(self._notification_router_loop())

    async def _notification_router_loop(self) -> None:
        try:
            while True:
                msg = await self._notification_queue.get()
                async with self._notification_condition:
                    self._notification_buffer.append(msg)
                    overflow = len(self._notification_buffer) - self._notification_buffer_limit
                    if overflow > 0:
                        del self._notification_buffer[:overflow]
                    self._notification_condition.notify_all()
        except asyncio.CancelledError:
            return

    async def _dispatch_server_request(self, msg: dict[str, Any]) -> None:
        try:
            await self._handle_server_request(msg)
        except asyncio.CancelledError:
            raise
        except CodexAgenticError as exc:
            req_id = msg.get("id")
            if not self._is_jsonrpc_request_id(req_id):
                return
            await self._send_server_request_error(req_id=req_id, code=-32000, message=str(exc))
        except Exception:
            req_id = msg.get("id")
            if not self._is_jsonrpc_request_id(req_id):
                return
            await self._send_server_request_error(req_id=req_id, code=-32603, message="Server request handler failed.")

    async def _send_server_request_error(self, *, req_id: str | int, code: int, message: str) -> None:
        try:
            await self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": code, "message": message},
                }
            )
        except Exception:
            return

    @staticmethod
    def _notification_matches_stream(notification: dict[str, Any], thread_id: str, turn_id: str | None) -> bool:
        note_thread_id = AsyncCodexAgenticClient._extract_thread_id_from_payload(notification)
        if note_thread_id and note_thread_id != thread_id:
            return False

        note_turn_id = AsyncCodexAgenticClient._extract_turn_id(notification)
        if turn_id and note_turn_id and note_turn_id != turn_id:
            return False
        return True

    @staticmethod
    def _is_jsonrpc_request_id(value: Any) -> bool:
        if isinstance(value, bool):
            return False
        return isinstance(value, (str, int))

    async def _wait_for_matching_notification(self, thread_id: str, turn_id: str | None) -> dict[str, Any]:
        self._ensure_notification_router()
        timeout = 5.0
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            async with self._notification_condition:
                for index, notification in enumerate(self._notification_buffer):
                    if self._notification_matches_stream(notification, thread_id, turn_id):
                        return self._notification_buffer.pop(index)

                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(self._notification_condition.wait(), timeout=remaining)

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = str(msg.get("method", ""))
        req_id = msg.get("id")
        if not self._is_jsonrpc_request_id(req_id):
            return
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        handlers: dict[str, Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "item/commandExecution/requestApproval": self._handle_command_approval_request,
            "item/fileChange/requestApproval": self._handle_file_change_request,
            "item/tool/requestUserInput": self._handle_tool_user_input_request,
            "item/tool/call": self._handle_tool_call_request,
        }
        handler = handlers.get(method)
        if handler is None:
            await self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unsupported server request method: {method}"},
                }
            )
            return

        result = await handler(method, params)
        await self._send_json({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _handle_command_approval_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method
        handler = self.on_command_approval
        if handler is None and self.policy_engine is not None:
            context = self._build_policy_context("command", "item/commandExecution/requestApproval", params)

            def _policy_handler(payload: dict[str, Any]) -> dict[str, Any] | Awaitable[dict[str, Any]]:
                assert self.policy_engine is not None
                return self.policy_engine.on_command_approval(payload, context)

            handler = _policy_handler
        return await self._resolve_server_request(
            "item/commandExecution/requestApproval",
            params,
            handler,
            {"decision": "accept"},
        )

    async def _handle_file_change_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method
        handler = self.on_file_change_approval
        if handler is None and self.policy_engine is not None:
            context = self._build_policy_context("file_change", "item/fileChange/requestApproval", params)

            def _policy_handler(payload: dict[str, Any]) -> dict[str, Any] | Awaitable[dict[str, Any]]:
                assert self.policy_engine is not None
                return self.policy_engine.on_file_change_approval(payload, context)

            handler = _policy_handler
        return await self._resolve_server_request(
            "item/fileChange/requestApproval",
            params,
            handler,
            {"decision": "accept"},
        )

    async def _handle_tool_user_input_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method
        handler = self.on_tool_request_user_input
        if handler is None and self.policy_engine is not None:
            context = self._build_policy_context("tool_user_input", "item/tool/requestUserInput", params)

            def _policy_handler(payload: dict[str, Any]) -> dict[str, Any] | Awaitable[dict[str, Any]]:
                assert self.policy_engine is not None
                return self.policy_engine.on_tool_request_user_input(payload, context)

            handler = _policy_handler
        return await self._resolve_server_request(
            "item/tool/requestUserInput",
            params,
            handler,
            {"answers": {}},
        )

    async def _handle_tool_call_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del method
        return await self._resolve_server_request(
            "item/tool/call",
            params,
            self.on_tool_call,
            {
                "success": False,
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": "No tool-call handler configured.",
                    }
                ],
            },
        )

    async def _resolve_server_request(
        self,
        method: str,
        params: dict[str, Any],
        handler: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]] | None,
        default_result: dict[str, Any],
    ) -> dict[str, Any]:
        if handler is None:
            return default_result
        try:
            maybe_result = handler(params)
            if inspect.isawaitable(maybe_result):
                maybe_result = await maybe_result
        except Exception as exc:
            raise CodexAgenticError(f"Handler failed for server request '{method}': {exc}") from exc
        if not isinstance(maybe_result, dict):
            raise CodexAgenticError(
                f"Handler for server request '{method}' must return a dict, got {type(maybe_result).__name__}."
            )
        return maybe_result

    @staticmethod
    def _build_policy_context(request_type: str, method: str, params: dict[str, Any]) -> "PolicyContext":
        from .policy import PolicyContext

        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item_id = params.get("itemId")
        return PolicyContext(
            request_type=request_type,  # type: ignore[arg-type]
            method=method,
            thread_id=thread_id if isinstance(thread_id, str) else None,
            turn_id=turn_id if isinstance(turn_id, str) else None,
            item_id=item_id if isinstance(item_id, str) else None,
            params=dict(params),
        )

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise AppServerConnectionError("App-server process is not running.")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send_json(payload)

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        ensure_connected: bool = True,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if ensure_connected:
            await self.connect()
        elif self._proc is None or self._proc.stdin is None:
            raise AppServerConnectionError("App-server process is not running.")
        req_id = self._next_id
        self._next_id += 1

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        try:
            await self._send_json(payload)
            if timeout_seconds is None:
                raw_resp = await fut
            else:
                raw_resp = await asyncio.wait_for(fut, timeout=timeout_seconds)
        except Exception:
            self._pending.pop(req_id, None)
            raise

        if "error" in raw_resp:
            self._raise_rpc_error(raw_resp.get("error"))
        result = raw_resp.get("result")
        if isinstance(result, dict):
            return result
        return {"value": result}

    @staticmethod
    def _raise_rpc_error(error: Any) -> None:
        msg = first_nonempty_text(error) or "App-server returned an RPC error."
        lower = msg.lower()
        if AsyncCodexAgenticClient._is_auth_error_text(msg):
            raise NotAuthenticatedError(AsyncCodexAgenticClient._not_authenticated_message(msg))
        if "not found" in lower and ("thread" in lower or "session" in lower):
            raise SessionNotFoundError(msg)
        raise CodexAgenticError(msg)

    @staticmethod
    def _is_no_rollout_found_error(exc: Exception) -> bool:
        lower = str(exc).lower()
        return "no rollout found" in lower or "rollout not found" in lower

    @staticmethod
    def _is_auth_error_text(text: str) -> bool:
        lower = text.lower()
        return any(token in lower for token in ("login", "auth", "credential", "unauthorized"))

    @staticmethod
    def _not_authenticated_message(detail: str) -> str:
        base = detail.strip() or "Codex authentication is unavailable or invalid."
        guidance = "Run 'codex login' in your terminal and retry."
        return f"{base} {guidance}"

    @staticmethod
    def _phase_for_method(method: str) -> str:
        if method == "turn/completed":
            return "completed"
        if method == "error":
            return "error"
        if method.startswith("item/agentMessage"):
            return "assistant"
        if method == "item/reasoning/summaryPartAdded":
            return "planning"
        if "reasoning" in method or method.startswith("turn/plan"):
            return "planning"
        if "/commandExecution/" in method or "/mcpToolCall/" in method or "/fileChange/" in method:
            return "tool"
        return "system"

    @staticmethod
    def _extract_thread_id_from_payload(payload: dict[str, Any]) -> str | None:
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        for key in ("threadId", "conversationId", "sessionId"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        thread = params.get("thread")
        if isinstance(thread, dict):
            value = thread.get("id")
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _extract_turn_id(payload: dict[str, Any]) -> str | None:
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        value = params.get("turnId")
        if isinstance(value, str) and value:
            return value
        turn = params.get("turn")
        if isinstance(turn, dict):
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id:
                return turn_id
        return None

    @staticmethod
    def _notification_to_event(payload: dict[str, Any]) -> ResponseEvent:
        method = str(payload.get("method", "unknown"))
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        parsed = AsyncCodexAgenticClient._parse_notification_fields(method, params)

        req = params.get("requestId")

        return ResponseEvent(
            type=method,
            phase=AsyncCodexAgenticClient._phase_for_method(method),
            text_delta=parsed.get("text_delta"),
            message_text=parsed.get("message_text"),
            request_id=req if isinstance(req, str) else None,
            session_id=AsyncCodexAgenticClient._extract_thread_id_from_payload(payload),
            turn_id=AsyncCodexAgenticClient._extract_turn_id(payload),
            item_id=parsed.get("item_id"),
            summary_index=parsed.get("summary_index"),
            thread_name=parsed.get("thread_name"),
            token_usage=parsed.get("token_usage"),
            plan=parsed.get("plan"),
            diff=parsed.get("diff"),
            raw=payload,
        )

    @staticmethod
    def _parse_notification_fields(method: str, params: dict[str, Any]) -> dict[str, Any]:
        parser_map: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "item/agentMessage/delta": AsyncCodexAgenticClient._parse_delta_notification,
            "item/reasoning/summaryTextDelta": AsyncCodexAgenticClient._parse_delta_notification,
            "item/reasoning/textDelta": AsyncCodexAgenticClient._parse_delta_notification,
            "item/commandExecution/outputDelta": AsyncCodexAgenticClient._parse_delta_notification,
            "item/fileChange/outputDelta": AsyncCodexAgenticClient._parse_delta_notification,
            "item/plan/delta": AsyncCodexAgenticClient._parse_delta_notification,
            "item/mcpToolCall/progress": AsyncCodexAgenticClient._parse_mcp_progress_notification,
            "thread/name/updated": AsyncCodexAgenticClient._parse_thread_name_notification,
            "thread/tokenUsage/updated": AsyncCodexAgenticClient._parse_token_usage_notification,
            "turn/diff/updated": AsyncCodexAgenticClient._parse_diff_notification,
            "turn/plan/updated": AsyncCodexAgenticClient._parse_plan_notification,
            "item/reasoning/summaryPartAdded": AsyncCodexAgenticClient._parse_summary_part_notification,
            "thread/compacted": AsyncCodexAgenticClient._parse_thread_compacted_notification,
            "item/completed": AsyncCodexAgenticClient._parse_item_completed_notification,
            "turn/completed": AsyncCodexAgenticClient._parse_turn_completed_notification,
            "deprecationNotice": AsyncCodexAgenticClient._parse_text_message_notification,
            "configWarning": AsyncCodexAgenticClient._parse_text_message_notification,
            "windows/worldWritableWarning": AsyncCodexAgenticClient._parse_text_message_notification,
            "authStatusChange": AsyncCodexAgenticClient._parse_text_message_notification,
            "account/updated": AsyncCodexAgenticClient._parse_text_message_notification,
            "account/rateLimits/updated": AsyncCodexAgenticClient._parse_text_message_notification,
        }
        parser = parser_map.get(method)
        if parser is not None:
            return parser(params)
        if method.startswith("codex/event/"):
            return AsyncCodexAgenticClient._parse_text_message_notification(params)
        return {}

    @staticmethod
    def _parse_delta_notification(params: dict[str, Any]) -> dict[str, Any]:
        delta = params.get("delta")
        if isinstance(delta, str):
            return {"text_delta": delta, "message_text": delta}
        return {}

    @staticmethod
    def _parse_mcp_progress_notification(params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message")
        if isinstance(message, str):
            return {"message_text": message}
        return {}

    @staticmethod
    def _parse_thread_name_notification(params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("threadName")
        if isinstance(name, str):
            return {"thread_name": name, "message_text": f"thread name: {name}"}
        if name is None:
            return {"message_text": "thread name cleared"}
        return {}

    @staticmethod
    def _parse_token_usage_notification(params: dict[str, Any]) -> dict[str, Any]:
        usage = params.get("tokenUsage")
        if not isinstance(usage, dict):
            return {}
        return {
            "token_usage": usage,
            "message_text": token_usage_summary(usage) or "token usage updated",
        }

    @staticmethod
    def _parse_diff_notification(params: dict[str, Any]) -> dict[str, Any]:
        current_diff = params.get("diff")
        if not isinstance(current_diff, str):
            return {}
        added, removed = diff_change_counts(current_diff)
        return {"diff": current_diff, "message_text": f"+{added} -{removed}"}

    @staticmethod
    def _parse_plan_notification(params: dict[str, Any]) -> dict[str, Any]:
        plan_value = params.get("plan")
        parsed_plan: list[dict[str, Any]] | None = None
        if isinstance(plan_value, list):
            parsed_plan = [entry for entry in plan_value if isinstance(entry, dict)]
        return {"plan": parsed_plan, "message_text": turn_plan_summary(params)}

    @staticmethod
    def _parse_summary_part_notification(params: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"message_text": "summary part added"}
        maybe_item_id = params.get("itemId")
        if isinstance(maybe_item_id, str):
            out["item_id"] = maybe_item_id
        maybe_summary_index = params.get("summaryIndex")
        if isinstance(maybe_summary_index, int):
            out["summary_index"] = maybe_summary_index
            out["message_text"] = f"summary part #{maybe_summary_index}"
        return out

    @staticmethod
    def _parse_thread_compacted_notification(params: dict[str, Any]) -> dict[str, Any]:
        del params
        return {"message_text": "thread compacted"}

    @staticmethod
    def _parse_text_message_notification(params: dict[str, Any]) -> dict[str, Any]:
        text = first_nonempty_text(params)
        if text:
            return {"message_text": text}
        return {}

    @staticmethod
    def _parse_item_completed_notification(params: dict[str, Any]) -> dict[str, Any]:
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return {}
        text = item.get("text")
        if isinstance(text, str):
            return {"message_text": text}
        return {}

    @staticmethod
    def _parse_turn_completed_notification(params: dict[str, Any]) -> dict[str, Any]:
        turn = params.get("turn")
        if not isinstance(turn, dict):
            return {}
        err = turn.get("error")
        text = first_nonempty_text(err)
        if text:
            return {"message_text": text}
        return {}

    @staticmethod
    def _merge_params(
        default_params: dict[str, Any],
        request_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(default_params)
        if request_params:
            for key, value in request_params.items():
                if value is not None:
                    merged[key] = value
        return merged

    async def responses_events(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        thread_params: dict[str, Any] | None = None,
        turn_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[ResponseEvent]:
        """Run one prompt and yield structured events in real time.

        Args:
            prompt: User prompt text.
            session_id: Existing thread id. If omitted, a new thread is started.
            thread_params: Per-request thread params merged over defaults.
            turn_params: Per-request turn params merged over defaults.

        Yields:
            ``ResponseEvent`` objects in arrival order.
        """

        merged_thread_params = self._merge_params(self.default_thread_params, thread_params)
        merged_turn_params = self._merge_params(self.default_turn_params, turn_params)

        if session_id is None:
            started = await self._request("thread/start", merged_thread_params)
            thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
            thread_id = thread.get("id") if isinstance(thread.get("id"), str) else None
            if not thread_id:
                raise CodexAgenticError("thread/start did not return a thread id.")
        else:
            thread_id = session_id
            try:
                resumed = await self._request(
                    "thread/resume",
                    {**merged_thread_params, "threadId": session_id},
                )
            except CodexAgenticError as exc:
                if not self._is_no_rollout_found_error(exc):
                    raise
            else:
                thread = resumed.get("thread") if isinstance(resumed.get("thread"), dict) else {}
                thread_id = thread.get("id") if isinstance(thread.get("id"), str) else session_id

        yield ResponseEvent(
            type="thread/ready",
            phase="system",
            session_id=thread_id,
            raw={"threadId": thread_id},
        )

        turn_start_params: dict[str, Any] = dict(merged_turn_params)
        turn_start_params["threadId"] = thread_id
        turn_start_params["input"] = [{"type": "text", "text": prompt, "text_elements": []}]
        turn_result = await self._request("turn/start", turn_start_params)
        turn = turn_result.get("turn") if isinstance(turn_result.get("turn"), dict) else {}
        turn_id = turn.get("id") if isinstance(turn.get("id"), str) else None
        idle_deadline: float | None = None
        if self.stream_idle_timeout_seconds is not None:
            idle_deadline = asyncio.get_running_loop().time() + max(0.0, self.stream_idle_timeout_seconds)

        while True:
            try:
                notification = await self._wait_for_matching_notification(thread_id, turn_id)
            except asyncio.TimeoutError:
                if self._proc is None:
                    raise AppServerConnectionError("App-server process is not running.")
                if self._proc.returncode is not None:
                    raise AppServerConnectionError("App-server process exited while waiting for notifications.")
                if not self._connected:
                    async with self._notification_condition:
                        if not self._notification_buffer:
                            raise AppServerConnectionError("App-server transport closed while waiting for notifications.")
                if idle_deadline is not None and asyncio.get_running_loop().time() >= idle_deadline:
                    raise AppServerConnectionError(
                        f"Timed out waiting for matching notifications for {self.stream_idle_timeout_seconds} seconds."
                    )
                continue
            if self.stream_idle_timeout_seconds is not None:
                idle_deadline = asyncio.get_running_loop().time() + max(0.0, self.stream_idle_timeout_seconds)
            method = str(notification.get("method", ""))

            event = self._notification_to_event(notification)
            if not event.session_id:
                event.session_id = thread_id
            yield event

            if method == "error":
                msg = first_nonempty_text(notification.get("params")) or "App-server emitted an error notification."
                raise CodexAgenticError(msg)

            if method == "turn/completed":
                params = notification.get("params") if isinstance(notification.get("params"), dict) else {}
                turn_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                status = turn_obj.get("status")
                if status == "failed":
                    err = turn_obj.get("error") if isinstance(turn_obj.get("error"), dict) else {}
                    msg = first_nonempty_text(err) or "Turn failed without detailed error."
                    raise CodexAgenticError(msg)
                break

    async def responses_stream_text(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        thread_params: dict[str, Any] | None = None,
        turn_params: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Run one prompt and yield text deltas only."""

        async for event in self.responses_events(
            prompt=prompt,
            session_id=session_id,
            thread_params=thread_params,
            turn_params=turn_params,
        ):
            if event.text_delta:
                yield event.text_delta

    async def responses_create(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        thread_params: dict[str, Any] | None = None,
        turn_params: dict[str, Any] | None = None,
        include_events: bool = False,
    ) -> AgentResponse:
        """Run one prompt and return the final aggregated response.

        Args:
            prompt: User prompt text.
            session_id: Existing thread id. If omitted, a new thread is started.
            thread_params: Per-request thread params merged over defaults.
            turn_params: Per-request turn params merged over defaults.
            include_events: If true, include all streamed ``ResponseEvent`` objects in the result.
        """

        chunks: list[str] = []
        assistant_chunks: list[str] = []
        assistant_message: str | None = None
        last_message: str | None = None
        thread_id: str | None = session_id
        request_id: str | None = None
        last_event_raw: dict[str, Any] | None = None
        events: list[ResponseEvent] | None = [] if include_events else None

        async for event in self.responses_events(
            prompt=prompt,
            session_id=session_id,
            thread_params=thread_params,
            turn_params=turn_params,
        ):
            last_event_raw = event.raw
            if events is not None:
                events.append(event)
            if event.session_id and not thread_id:
                thread_id = event.session_id
            if event.request_id:
                request_id = event.request_id
            if event.text_delta:
                chunks.append(event.text_delta)
                if event.type == "item/agentMessage/delta":
                    assistant_chunks.append(event.text_delta)
            extracted_assistant = self._extract_assistant_text_from_event(event)
            if extracted_assistant:
                assistant_message = extracted_assistant
            if event.message_text:
                last_message = event.message_text

        if not thread_id:
            raise CodexAgenticError("No thread id was produced by app-server.")

        text = (
            assistant_message
            or "".join(assistant_chunks).strip()
            or last_message
            or "".join(chunks).strip()
        )
        return AgentResponse(
            text=text,
            session_id=thread_id,
            request_id=request_id,
            tool_name="app-server",
            raw=last_event_raw or {},
            events=events,
        )

    @staticmethod
    def _extract_assistant_text_from_event(event: ResponseEvent) -> str | None:
        if event.type == "codex/event/agent_message":
            return first_nonempty_text(event.message_text)

        if event.type != "item/completed":
            return None

        raw = event.raw if isinstance(event.raw, dict) else {}
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        if item.get("type") != "agentMessage":
            return None

        text = item.get("text")
        if isinstance(text, str) and text:
            return text
        return None

    async def thread_start(
        self,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new thread via ``thread/start``."""

        return await self._request("thread/start", self._merge_params(self.default_thread_params, params))

    async def thread_read(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        """Read one thread by id."""

        result = await self._request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else None
        if thread is None:
            raise SessionNotFoundError(f"Unknown thread_id: {thread_id}")
        return thread

    async def thread_list(self, limit: int = 50, *, sort_key: str = "updated_at") -> list[dict[str, Any]]:
        """List available threads."""

        result = await self._request("thread/list", {"limit": limit, "sortKey": sort_key})
        data = result.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def thread_archive(self, thread_id: str) -> dict[str, Any]:
        """Archive one thread."""

        return await self._request("thread/archive", {"threadId": thread_id})

    async def thread_fork(
        self,
        thread_id: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = self._merge_params(self.default_thread_params, params)
        return await self._request("thread/fork", {**merged, "threadId": thread_id})

    async def thread_name_set(self, thread_id: str, name: str) -> dict[str, Any]:
        return await self._request("thread/name/set", {"threadId": thread_id, "name": name})

    async def thread_unarchive(self, thread_id: str) -> dict[str, Any]:
        return await self._request("thread/unarchive", {"threadId": thread_id})

    async def thread_compact_start(self, thread_id: str) -> dict[str, Any]:
        return await self._request("thread/compact/start", {"threadId": thread_id})

    async def thread_rollback(self, thread_id: str, num_turns: int) -> dict[str, Any]:
        return await self._request("thread/rollback", {"threadId": thread_id, "numTurns": num_turns})

    async def thread_loaded_list(self, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("thread/loaded/list", params)

    async def skills_list(
        self,
        *,
        cwds: list[str] | None = None,
        force_reload: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwds is not None:
            params["cwds"] = cwds
        if force_reload is not None:
            params["forceReload"] = force_reload
        return await self._request("skills/list", params)

    async def skills_remote_read(self) -> dict[str, Any]:
        return await self._request("skills/remote/read", {})

    async def skills_remote_write(self, hazelnut_id: str, is_preload: bool) -> dict[str, Any]:
        return await self._request(
            "skills/remote/write",
            {"hazelnutId": hazelnut_id, "isPreload": is_preload},
        )

    async def app_list(self, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("app/list", params)

    async def skills_config_write(self, path: str, enabled: bool) -> dict[str, Any]:
        return await self._request("skills/config/write", {"path": path, "enabled": enabled})

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def turn_steer(self, thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        return await self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
        )

    async def review_start(
        self,
        thread_id: str,
        target: dict[str, Any],
        *,
        delivery: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "target": target}
        if delivery is not None:
            params["delivery"] = delivery
        return await self._request("review/start", params)

    async def model_list(self, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("model/list", params)

    async def account_rate_limits_read(self) -> dict[str, Any]:
        return await self._request("account/rateLimits/read", None)

    async def account_read(self, *, refresh_token: bool | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if refresh_token is not None:
            params["refreshToken"] = refresh_token
        return await self._request("account/read", params)

    async def command_exec(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int | None = None,
        sandbox_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"command": command}
        request_timeout_seconds: float | None = None
        if cwd is not None:
            params["cwd"] = cwd
        if timeout_ms is not None:
            params["timeoutMs"] = timeout_ms
            # Honor the caller's command timeout and leave room for transport overhead.
            request_timeout_seconds = max(0.0, timeout_ms / 1000.0) + 30.0
        if sandbox_policy is not None:
            params["sandboxPolicy"] = sandbox_policy
        return await self._request(
            "command/exec",
            params,
            timeout_seconds=request_timeout_seconds,
        )

    async def config_read(self, *, cwd: str | None = None, include_layers: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd is not None:
            params["cwd"] = cwd
        if include_layers:
            params["includeLayers"] = include_layers
        return await self._request("config/read", params)

    async def config_value_write(
        self,
        key_path: str,
        value: Any,
        *,
        merge_strategy: str = "upsert",
        file_path: str | None = None,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "keyPath": key_path,
            "value": value,
            "mergeStrategy": merge_strategy,
        }
        if file_path is not None:
            params["filePath"] = file_path
        if expected_version is not None:
            params["expectedVersion"] = expected_version
        return await self._request("config/value/write", params)

    async def config_batch_write(
        self,
        edits: list[dict[str, Any]],
        *,
        file_path: str | None = None,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"edits": edits}
        if file_path is not None:
            params["filePath"] = file_path
        if expected_version is not None:
            params["expectedVersion"] = expected_version
        return await self._request("config/batchWrite", params)

    async def config_requirements_read(self) -> dict[str, Any]:
        return await self._request("configRequirements/read", None)

    async def config_mcp_server_reload(self) -> dict[str, Any]:
        return await self._request("config/mcpServer/reload", None)

    async def mcp_server_status_list(self, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("mcpServerStatus/list", params)

    async def mcp_server_oauth_login(
        self,
        name: str,
        *,
        scopes: list[str] | None = None,
        timeout_secs: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if scopes is not None:
            params["scopes"] = scopes
        if timeout_secs is not None:
            params["timeoutSecs"] = timeout_secs
        return await self._request("mcpServer/oauth/login", params)

    async def fuzzy_file_search(
        self,
        query: str,
        *,
        roots: list[str] | None = None,
        cancellation_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "query": query,
            "roots": roots[:] if roots is not None else [self.process_cwd],
        }
        if cancellation_token is not None:
            params["cancellationToken"] = cancellation_token
        return await self._request("fuzzyFileSearch", params)

    @staticmethod
    def _raise_connection_error(exc: Exception) -> None:
        text = str(exc)
        if AsyncCodexAgenticClient._is_auth_error_text(text):
            raise NotAuthenticatedError(AsyncCodexAgenticClient._not_authenticated_message(text)) from exc
        raise AppServerConnectionError(text) from exc
