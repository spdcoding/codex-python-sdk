from __future__ import annotations

import os
import re
import sys
from typing import Any, Iterator, TextIO

from ._shared import first_nonempty_text
from .types import ResponseEvent


class ExecStyleRenderer:
    """Render `ResponseEvent` stream in a compact CLI style similar to `codex exec`."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        show_reasoning: bool = True,
        show_system: bool = False,
        show_tool_output: bool = True,
        show_turn_summary: bool = True,
        color: str = "auto",
    ) -> None:
        self.stream = stream or sys.stdout
        self.show_reasoning = show_reasoning
        self.show_system = show_system
        self.show_tool_output = show_tool_output
        self.show_turn_summary = show_turn_summary

        self._in_assistant_line = False
        self._in_reasoning_line = False
        self._in_tool_output = False
        self._tool_items: dict[str, str] = {}
        self._turn_tool_count = 0
        self._turn_error_count = 0
        self._seen_deprecation_warnings: set[str] = set()

        self._color_mode = self._normalize_color_mode(color)
        self._use_color = self._compute_use_color(self._color_mode, self.stream)
        self._palette = self._palette_for(self._color_mode)

    def render(self, event: ResponseEvent) -> None:
        method = event.type
        params = event.raw.get("params") if isinstance(event.raw.get("params"), dict) else {}

        if method.startswith("codex/event/"):
            self._render_codex_event(method, params, event)
            return

        if self._render_lifecycle_event(method, params, event):
            return
        if self._render_item_event(method, params, event):
            return
        if self._render_account_event(method, params, event):
            return

        if self.show_system:
            self._close_inline_sections()
            msg = event.message_text or event.text_delta or ""
            tail = f" {msg}" if msg else ""
            self._println(f"{self._tone('system', '[event]')} {method}{tail}")

    def _render_lifecycle_event(self, method: str, params: dict[str, Any], event: ResponseEvent) -> bool:
        if method == "thread/ready":
            self._close_inline_sections()
            session = event.session_id or "unknown"
            self._println(f"{self._tone('session', 'Session:')} {session}")
            return True
        if method == "thread/started":
            if self.show_system:
                self._close_inline_sections()
                self._println(f"{self._tone('system', '[thread]')} started")
            return True
        if method == "thread/name/updated":
            if self.show_system:
                self._close_inline_sections()
                name = event.thread_name
                if name is None:
                    self._println(f"{self._tone('system', '[thread.name]')} (cleared)")
                else:
                    self._println(f"{self._tone('system', '[thread.name]')} {name}")
            return True
        if method == "thread/tokenUsage/updated":
            if self.show_system:
                self._close_inline_sections()
                msg = event.message_text or "token usage updated"
                self._println(f"{self._tone('system', '[usage]')} {msg}")
            return True
        if method == "thread/compacted":
            if self.show_system:
                self._close_inline_sections()
                self._println(f"{self._tone('system', '[context]')} compacted")
            return True
        if method == "turn/started":
            self._close_inline_sections()
            self._println("")
            self._println(self._tone("header", "== Turn Started =="))
            self._turn_tool_count = 0
            self._turn_error_count = 0
            self._tool_items.clear()
            return True
        if method == "turn/completed":
            self._close_inline_sections()
            self._println("")
            if self.show_turn_summary:
                status = self._turn_status(params)
                summary = f"status={status}, tools={self._turn_tool_count}, errors={self._turn_error_count}"
                self._println(f"{self._tone('summary', '[summary]')} {summary}")
            self._println(self._tone("header", "== Turn Completed =="))
            return True
        if method == "error":
            self._close_inline_sections()
            msg = event.message_text or first_nonempty_text(params) or "unknown error"
            self._println(f"{self._tone('error', '[error]')} {msg}")
            self._turn_error_count += 1
            return True
        if method == "deprecationNotice":
            self._render_deprecation_warning(method, params, event)
            return True
        if method in ("configWarning", "windows/worldWritableWarning"):
            self._close_inline_sections()
            msg = event.message_text or first_nonempty_text(params) or method
            self._println(f"{self._tone('warning', '[warning]')} {msg}")
            return True
        return False

    def _render_item_event(self, method: str, params: dict[str, Any], event: ResponseEvent) -> bool:
        if method == "item/started":
            self._render_item_started(params)
            return True
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            self._render_reasoning_delta(event)
            return True
        if method in ("item/commandExecution/outputDelta", "item/fileChange/outputDelta"):
            self._render_tool_output_delta(params, event)
            return True
        if method == "item/commandExecution/terminalInteraction":
            self._render_terminal_interaction(params)
            return True
        if method == "item/mcpToolCall/progress":
            self._render_mcp_progress(params, event)
            return True
        if method == "item/plan/delta":
            self._render_plan_delta(event)
            return True
        if method == "turn/plan/updated":
            self._render_turn_plan(event)
            return True
        if method == "turn/diff/updated":
            self._render_turn_diff(event)
            return True
        if method == "item/reasoning/summaryPartAdded":
            self._render_summary_part(event)
            return True
        if method == "item/agentMessage/delta":
            self._render_agent_delta(event)
            return True
        if method == "item/completed":
            self._render_item_completed(params, event)
            return True
        return False

    def _render_account_event(self, method: str, params: dict[str, Any], event: ResponseEvent) -> bool:
        if method not in ("account/rateLimits/updated", "account/updated", "authStatusChange"):
            return False
        if self.show_system:
            self._close_inline_sections()
            msg = event.message_text or first_nonempty_text(params) or method
            self._println(f"{self._tone('system', '[account]')} {msg}")
        return True

    def _render_item_started(self, params: dict[str, Any]) -> None:
        self._close_inline_sections()
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        item_type = item.get("type")
        item_id = item.get("id") if isinstance(item.get("id"), str) else None
        if item_id:
            self._tool_items[item_id] = self._label_for_item(item)
        if item_type == "commandExecution":
            command = item.get("command")
            if isinstance(command, str) and command:
                self._println(f"{self._tone('tool', '[tool.exec]')} {command}")
            else:
                self._println(self._tone("tool", "[tool.exec] started"))
            self._turn_tool_count += 1
            return
        if item_type == "mcpToolCall":
            server = item.get("server")
            tool = item.get("tool")
            if isinstance(server, str) and isinstance(tool, str):
                self._println(f"{self._tone('tool', '[tool.mcp]')} {server}/{tool}")
            else:
                self._println(self._tone("tool", "[tool.mcp] started"))
            self._turn_tool_count += 1
            return
        if item_type == "fileChange":
            self._println(self._tone("tool", "[tool.patch] applying file changes"))
            self._turn_tool_count += 1
            return
        if item_type == "webSearch":
            query = item.get("query")
            if isinstance(query, str) and query:
                self._println(f"{self._tone('tool', '[tool.web]')} {query}")
            else:
                self._println(self._tone("tool", "[tool.web] searching"))
            self._turn_tool_count += 1
            return
        if item_type == "plan":
            if self.show_system:
                self._println(self._tone("plan", "[plan] started"))
            return
        if item_type == "reasoning":
            if self.show_reasoning and self.show_system:
                self._println(self._tone("thinking", "[thinking] started"))
            return
        if self.show_system:
            self._println(f"{self._tone('system', '[item]')} {item_type or 'unknown'} started")

    def _render_reasoning_delta(self, event: ResponseEvent) -> None:
        if not self.show_reasoning:
            return
        delta = event.text_delta or event.message_text
        if not delta:
            return
        self._close_assistant()
        self._close_tool_output()
        if not self._in_reasoning_line:
            self._print(self._tone("thinking", "[thinking] "))
            self._in_reasoning_line = True
        self._print(delta)

    def _render_tool_output_delta(self, params: dict[str, Any], event: ResponseEvent) -> None:
        if not self.show_tool_output:
            return
        delta = event.text_delta or event.message_text
        if not delta:
            return
        self._close_assistant()
        self._close_reasoning()
        if not self._in_tool_output:
            label = self._label_for_item_id(params.get("itemId"))
            self._println(self._tone("tool_output", f"[tool.output:{label}]"))
            self._in_tool_output = True
        self._print(delta)

    def _render_terminal_interaction(self, params: dict[str, Any]) -> None:
        self._close_inline_sections()
        user_stdin = params.get("stdin")
        if isinstance(user_stdin, str) and user_stdin:
            self._println(f"{self._tone('tool', '[tool.stdin]')} {user_stdin.rstrip()}")
        elif self.show_system:
            self._println(self._tone("tool", "[tool.stdin] input sent"))

    def _render_mcp_progress(self, params: dict[str, Any], event: ResponseEvent) -> None:
        msg = event.message_text or first_nonempty_text(params)
        if not msg:
            return
        self._close_inline_sections()
        self._println(f"{self._tone('tool', '[tool.mcp.progress]')} {msg}")

    def _render_plan_delta(self, event: ResponseEvent) -> None:
        if not self.show_system:
            return
        delta = event.text_delta or event.message_text
        if not delta:
            return
        self._close_inline_sections()
        self._println(f"{self._tone('plan', '[plan]')} {delta}")

    def _render_turn_plan(self, event: ResponseEvent) -> None:
        if not self.show_system:
            return
        msg = event.message_text
        if not msg:
            return
        self._close_inline_sections()
        self._println(f"{self._tone('plan', '[turn.plan]')} {msg}")

    def _render_turn_diff(self, event: ResponseEvent) -> None:
        if not self.show_system:
            return
        msg = event.message_text
        if not msg:
            return
        self._close_inline_sections()
        self._println(f"{self._tone('system', '[turn.diff]')} {msg}")

    def _render_summary_part(self, event: ResponseEvent) -> None:
        if not self.show_reasoning:
            return
        self._close_assistant()
        self._close_tool_output()
        self._close_reasoning()
        idx = event.summary_index
        if idx is None:
            self._println(self._tone("thinking", "[thinking] summary part added"))
        else:
            self._println(self._tone("thinking", f"[thinking] summary part #{idx}"))

    def _render_agent_delta(self, event: ResponseEvent) -> None:
        delta = event.text_delta or event.message_text
        if not delta:
            return
        self._close_reasoning()
        self._close_tool_output()
        if not self._in_assistant_line:
            self._print(self._tone("assistant", "Assistant: "))
            self._in_assistant_line = True
        self._print(delta)

    def _render_item_completed(self, params: dict[str, Any], event: ResponseEvent) -> None:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        if item.get("type") == "agentMessage":
            if not self._in_assistant_line and event.message_text:
                self._close_reasoning()
                self._close_tool_output()
                self._print(self._tone("assistant", "Assistant: "))
                self._print(event.message_text)
                self._in_assistant_line = True
            return
        if item.get("type") == "commandExecution":
            self._close_inline_sections()
            status = item.get("status")
            exit_code = item.get("exitCode")
            duration_ms = item.get("durationMs")
            status_text = str(status) if isinstance(status, str) else "completed"
            suffix: list[str] = [status_text]
            if isinstance(exit_code, int):
                suffix.append(f"exit={exit_code}")
            if isinstance(duration_ms, int):
                suffix.append(f"{duration_ms}ms")
            self._println(f"{self._tone('tool', '[tool.exec.done]')} {', '.join(suffix)}")
            return
        if item.get("type") == "mcpToolCall":
            self._close_inline_sections()
            status = item.get("status")
            status_text = str(status) if isinstance(status, str) else "completed"
            self._println(f"{self._tone('tool', '[tool.mcp.done]')} {status_text}")
            return
        if item.get("type") == "fileChange":
            self._close_inline_sections()
            status = item.get("status")
            status_text = str(status) if isinstance(status, str) else "completed"
            self._println(f"{self._tone('tool', '[tool.patch.done]')} {status_text}")
            return
        if self.show_system:
            self._close_inline_sections()
            self._println(f"{self._tone('system', '[item]')} {item.get('type', 'unknown')} completed")

    def finish(self) -> None:
        self._close_inline_sections()

    def _close_inline_sections(self) -> None:
        self._close_assistant()
        self._close_reasoning()
        self._close_tool_output()

    def _close_assistant(self) -> None:
        if self._in_assistant_line:
            self._println("")
            self._in_assistant_line = False

    def _close_reasoning(self) -> None:
        if self._in_reasoning_line:
            self._println("")
            self._in_reasoning_line = False

    def _close_tool_output(self) -> None:
        if self._in_tool_output:
            self._println("")
            self._in_tool_output = False

    def _print(self, text: str) -> None:
        self.stream.write(text)
        self.stream.flush()

    def _println(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def _label_for_item(self, item: dict[str, Any]) -> str:
        item_type = item.get("type")
        if item_type == "commandExecution":
            return "exec"
        if item_type == "fileChange":
            return "patch"
        if item_type == "webSearch":
            return "web"
        if item_type == "mcpToolCall":
            server = item.get("server")
            tool = item.get("tool")
            if isinstance(server, str) and isinstance(tool, str):
                return f"mcp:{server}/{tool}"
            return "mcp"
        if isinstance(item_type, str) and item_type:
            return item_type
        return "tool"

    def _label_for_item_id(self, item_id: Any) -> str:
        if isinstance(item_id, str):
            return self._tool_items.get(item_id, "tool")
        return "tool"

    @staticmethod
    def _turn_status(params: dict[str, Any]) -> str:
        turn = params.get("turn")
        if isinstance(turn, dict):
            status = turn.get("status")
            if isinstance(status, str) and status:
                return status
        return "completed"

    def _render_codex_event(self, method: str, params: dict[str, Any], event: ResponseEvent) -> None:
        suffix = method.removeprefix("codex/event/")

        # `codex/event/*` frequently duplicates canonical `item/*` events.
        if suffix in {
            "reasoning_content_delta",
            "agent_reasoning_delta",
            "agent_message_content_delta",
            "item_started",
            "item_completed",
            "task_started",
            "task_complete",
        }:
            return

        if suffix == "deprecation_notice":
            self._render_deprecation_warning(method, params, event)
            return

        if suffix == "stream_error":
            self._close_inline_sections()
            msg = event.message_text or first_nonempty_text(params) or suffix
            self._println(f"{self._tone('error', '[error]')} {msg}")
            self._turn_error_count += 1
            return

        if self.show_system:
            self._close_inline_sections()
            msg = event.message_text or event.text_delta or first_nonempty_text(params) or ""
            tail = f" {msg}" if msg else ""
            self._println(f"{self._tone('system', f'[codex.{suffix}]')}{tail}")

    def _render_deprecation_warning(self, source_method: str, params: dict[str, Any], event: ResponseEvent) -> None:
        self._close_inline_sections()
        message = self._format_deprecation_warning(source_method, params, event)
        if not message:
            return
        key = self._normalize_deprecation_key(message)
        if key in self._seen_deprecation_warnings:
            return
        self._seen_deprecation_warnings.add(key)
        self._println(f"{self._tone('warning', '[warning]')} {message}")

    @staticmethod
    def _normalize_deprecation_key(message: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", message.lower())

    @staticmethod
    def _find_first_string_for_keys(value: Any, keys: tuple[str, ...]) -> str | None:
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for nested in value.values():
                text = ExecStyleRenderer._find_first_string_for_keys(nested, keys)
                if text:
                    return text
        elif isinstance(value, list):
            for item in value:
                text = ExecStyleRenderer._find_first_string_for_keys(item, keys)
                if text:
                    return text
        return None

    def _format_deprecation_warning(self, source_method: str, params: dict[str, Any], event: ResponseEvent) -> str | None:
        raw_message = event.message_text or first_nonempty_text(params) or ""
        normalized = self._normalize_deprecation_key(raw_message)
        if normalized and normalized not in {"deprecationnotice", "deprecated"}:
            return raw_message

        target = self._find_first_string_for_keys(
            params,
            (
                "method",
                "event",
                "name",
                "interface",
                "api",
                "path",
                "deprecated",
            ),
        )
        replacement = self._find_first_string_for_keys(
            params,
            ("replacement", "newMethod", "newEvent", "instead", "use"),
        )
        if target and replacement:
            return f"{target} is deprecated; use {replacement}"
        if target:
            return f"{target} is deprecated"
        return None

    def _tone(self, tone: str, text: str) -> str:
        if not self._use_color:
            return text
        code = self._palette.get(tone)
        if not code:
            return text
        return f"{code}{text}\x1b[0m"

    @staticmethod
    def _normalize_color_mode(color: str) -> str:
        mode = str(color or "auto").strip().lower()
        alias = {"on": "soft", "true": "soft", "false": "off", "none": "off"}
        mode = alias.get(mode, mode)
        if mode not in {"auto", "off", "soft", "vivid"}:
            return "auto"
        return mode

    @staticmethod
    def _compute_use_color(mode: str, stream: TextIO) -> bool:
        if mode == "off":
            return False
        if mode in {"soft", "vivid"}:
            return True

        if os.environ.get("NO_COLOR") is not None:
            return False
        if os.environ.get("CLICOLOR_FORCE") == "1":
            return True

        isatty = getattr(stream, "isatty", None)
        if callable(isatty):
            try:
                return bool(isatty())
            except Exception:
                return False
        return False

    @staticmethod
    def _palette_for(mode: str) -> dict[str, str]:
        if mode == "vivid":
            return {
                "header": "\x1b[1;96m",
                "session": "\x1b[1;36m",
                "assistant": "\x1b[1;32m",
                "thinking": "\x1b[1;35m",
                "tool": "\x1b[1;33m",
                "tool_output": "\x1b[1;94m",
                "summary": "\x1b[1;34m",
                "warning": "\x1b[1;93m",
                "error": "\x1b[1;91m",
                "system": "\x1b[2;37m",
                "plan": "\x1b[1;34m",
            }
        return {
            "header": "\x1b[36m",
            "session": "\x1b[36m",
            "assistant": "\x1b[32m",
            "thinking": "\x1b[35m",
            "tool": "\x1b[33m",
            "tool_output": "\x1b[34m",
            "summary": "\x1b[34m",
            "warning": "\x1b[33m",
            "error": "\x1b[31m",
            "system": "\x1b[2m",
            "plan": "\x1b[34m",
        }


def render_exec_style_events(
    events: Iterator[ResponseEvent],
    *,
    stream: TextIO | None = None,
    show_reasoning: bool = True,
    show_system: bool = False,
    show_tool_output: bool = True,
    show_turn_summary: bool = True,
    color: str = "auto",
) -> None:
    """Render streaming events in a compact `codex exec`-like terminal style.

    Args:
        events: Iterator from ``responses_events``.
        stream: Output stream; defaults to ``sys.stdout``.
        show_reasoning: Whether to show reasoning deltas.
        show_system: Whether to show system events.
        show_tool_output: Whether to show command/file output deltas.
        show_turn_summary: Whether to print turn summary at completion.
        color: ``auto``, ``off``, ``soft``, or ``vivid``.
    """

    renderer = ExecStyleRenderer(
        stream=stream,
        show_reasoning=show_reasoning,
        show_system=show_system,
        show_tool_output=show_tool_output,
        show_turn_summary=show_turn_summary,
        color=color,
    )
    for event in events:
        renderer.render(event)
    renderer.finish()
