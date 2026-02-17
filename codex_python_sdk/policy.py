from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol

from ._shared import first_nonempty_text, utc_now
from .errors import AppServerConnectionError, CodexAgenticError, NotAuthenticatedError

if TYPE_CHECKING:
    from .async_client import AsyncCodexAgenticClient

RequestType = Literal["command", "file_change", "tool_user_input"]
Decision = Literal["accept", "acceptForSession", "decline", "cancel"]
PolicyMode = Literal["permissive", "balanced", "strict"]


@dataclass
class PolicyContext:
    request_type: RequestType
    method: str
    thread_id: str | None
    turn_id: str | None
    item_id: str | None
    params: dict[str, Any]
    timestamp: str = field(default_factory=utc_now)


@dataclass
class PolicyJudgeConfig:
    timeout_seconds: float = 8.0
    model: str | None = None
    effort: str | None = "medium"
    summary: str | None = "none"
    include_params: bool = True
    max_params_chars: int = 8000
    enable_web_search: bool = False
    fallback_command_decision: Decision = "decline"
    fallback_file_change_decision: Decision = "decline"


@dataclass
class PolicyRubric:
    system_rubric: str
    mode: PolicyMode = "permissive"
    use_llm_judge: bool = False
    command_rules: list[dict[str, Any]] = field(default_factory=list)
    file_change_rules: list[dict[str, Any]] = field(default_factory=list)
    tool_input_rules: list[dict[str, Any]] = field(default_factory=list)
    defaults: dict[str, Any] = field(
        default_factory=lambda: {
            "command": "accept",
            "file_change": "accept",
            "tool_input": "auto_empty",
        }
    )
    audit: dict[str, Any] = field(default_factory=lambda: {"enabled": False, "include_params": False})


DEFAULT_POLICY_RUBRIC = PolicyRubric(
    system_rubric=(
        "You are a policy judge for command/file-change approvals. "
        "When uncertain, prefer safe behavior."
    ),
    mode="permissive",
    use_llm_judge=False,
)


def _default_policy_rubric() -> PolicyRubric:
    # Avoid leaking mutations across engines by sharing a global mutable instance.
    return copy.deepcopy(DEFAULT_POLICY_RUBRIC)


class PolicyEngine(Protocol):
    def on_command_approval(
        self,
        params: dict[str, Any],
        context: PolicyContext,
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]:
        ...

    def on_file_change_approval(
        self,
        params: dict[str, Any],
        context: PolicyContext,
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]:
        ...

    def on_tool_request_user_input(
        self,
        params: dict[str, Any],
        context: PolicyContext,
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]:
        ...


class DefaultPolicyEngine:
    """Default behavior: mirror current client defaults."""

    def on_command_approval(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        del params, context
        return {"decision": "accept"}

    def on_file_change_approval(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        del params, context
        return {"decision": "accept"}

    def on_tool_request_user_input(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        del params, context
        return {"answers": {}}


class RuleBasedPolicyEngine:
    """Rule evaluator based on a declarative rubric dict/dataclass."""

    def __init__(self, rubric: PolicyRubric | dict[str, Any] | None = None) -> None:
        self.rubric = _to_rubric(rubric) if rubric is not None else _default_policy_rubric()

    def on_command_approval(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        fallback = self.rubric.defaults.get("command", "accept")
        decision = self._apply_rules(
            self.rubric.command_rules,
            params,
            context,
            fallback=fallback,
            kind="command",
        )
        return _normalize_command_or_file_decision(decision, fallback=str(fallback))

    def on_file_change_approval(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        fallback = self.rubric.defaults.get("file_change", "accept")
        decision = self._apply_rules(
            self.rubric.file_change_rules,
            params,
            context,
            fallback=fallback,
            kind="file_change",
        )
        return _normalize_command_or_file_decision(decision, fallback=str(fallback))

    def on_tool_request_user_input(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        decision = self._apply_rules(
            self.rubric.tool_input_rules,
            params,
            context,
            fallback=self.rubric.defaults.get("tool_input", "auto_empty"),
            kind="tool_user_input",
        )
        return _normalize_user_input_decision(decision)

    def _apply_rules(
        self,
        rules: list[dict[str, Any]],
        params: dict[str, Any],
        context: PolicyContext,
        *,
        fallback: Any,
        kind: RequestType,
    ) -> Any:
        ordered = sorted(rules, key=lambda item: int(item.get("priority", 1000)))
        for rule in ordered:
            when = rule.get("when", {})
            if not isinstance(when, dict):
                continue
            if _matches_rule(when, params, context, kind=kind):
                return rule.get("decision", fallback)
        return fallback


class LlmRubricPolicyEngine:
    """Policy engine that asks an LLM judge using a temporary app-server thread."""

    def __init__(
        self,
        *,
        rubric: PolicyRubric | dict[str, Any] | None = None,
        judge_config: PolicyJudgeConfig | None = None,
        codex_command: str = "codex",
        app_server_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        process_cwd: str | None = None,
        server_config_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.rubric = _to_rubric(rubric) if rubric is not None else _default_policy_rubric()
        self.judge_config = judge_config or PolicyJudgeConfig()
        self.codex_command = codex_command
        self.app_server_args = app_server_args[:] if app_server_args else ["app-server"]
        self.env = env
        self.process_cwd = process_cwd
        self.server_config_overrides = dict(server_config_overrides or {})
        self._judge_client: AsyncCodexAgenticClient | None = None
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._judge_client_lock_ref: asyncio.Lock | None = None
        self._judge_call_lock_ref: asyncio.Lock | None = None

    def _ensure_runtime_locks(self) -> None:
        loop = asyncio.get_running_loop()
        if self._runtime_loop is None:
            self._runtime_loop = loop
        elif self._runtime_loop is not loop:
            raise CodexAgenticError("Policy engine cannot be shared across different event loops.")

        if self._judge_client_lock_ref is None:
            self._judge_client_lock_ref = asyncio.Lock()
        if self._judge_call_lock_ref is None:
            self._judge_call_lock_ref = asyncio.Lock()

    @property
    def _judge_client_lock(self) -> asyncio.Lock:
        if self._judge_client_lock_ref is None:
            self._ensure_runtime_locks()
        assert self._judge_client_lock_ref is not None
        return self._judge_client_lock_ref

    @property
    def _judge_call_lock(self) -> asyncio.Lock:
        if self._judge_call_lock_ref is None:
            self._ensure_runtime_locks()
        assert self._judge_call_lock_ref is not None
        return self._judge_call_lock_ref

    async def aclose(self) -> None:
        """Close the internal judge client if it exists."""

        client: AsyncCodexAgenticClient | None
        async with self._judge_client_lock:
            client = self._judge_client
            self._judge_client = None
        if client is not None:
            await client.close()

    async def on_command_approval(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        allowed = ["accept", "acceptForSession", "decline", "cancel"]
        fallback = {"decision": self.judge_config.fallback_command_decision}
        return await self._judge_or_fallback(
            params=params,
            context=context,
            allowed_decisions=allowed,
            fallback=fallback,
            output_kind="command_or_file",
        )

    async def on_file_change_approval(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        allowed = ["accept", "acceptForSession", "decline", "cancel"]
        fallback = {"decision": self.judge_config.fallback_file_change_decision}
        return await self._judge_or_fallback(
            params=params,
            context=context,
            allowed_decisions=allowed,
            fallback=fallback,
            output_kind="command_or_file",
        )

    async def on_tool_request_user_input(self, params: dict[str, Any], context: PolicyContext) -> dict[str, Any]:
        return await self._judge_or_fallback(
            params=params,
            context=context,
            allowed_decisions=[],
            fallback={"answers": {}},
            output_kind="user_input",
        )

    async def _judge_or_fallback(
        self,
        *,
        params: dict[str, Any],
        context: PolicyContext,
        allowed_decisions: list[str],
        fallback: dict[str, Any],
        output_kind: Literal["command_or_file", "user_input"],
    ) -> dict[str, Any]:
        try:
            payload = await asyncio.wait_for(
                self._judge_with_llm(
                    params=params,
                    context=context,
                    allowed_decisions=allowed_decisions,
                    output_kind=output_kind,
                ),
                timeout=self.judge_config.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return fallback
        except (AppServerConnectionError, NotAuthenticatedError, CodexAgenticError):
            return fallback

        if output_kind == "command_or_file":
            return _normalize_command_or_file_decision(payload, fallback=fallback["decision"])
        return _normalize_user_input_decision(payload)

    async def _judge_with_llm(
        self,
        *,
        params: dict[str, Any],
        context: PolicyContext,
        allowed_decisions: list[str],
        output_kind: Literal["command_or_file", "user_input"],
    ) -> dict[str, Any]:
        input_payload = _build_judge_input(
            params=params,
            context=context,
            include_params=self.judge_config.include_params,
            max_params_chars=self.judge_config.max_params_chars,
        )
        prompt = _build_judge_prompt(
            rubric=self.rubric.system_rubric,
            context=context,
            allowed_decisions=allowed_decisions,
            input_payload=input_payload,
            output_kind=output_kind,
        )

        turn_params: dict[str, Any] = {
            "outputSchema": _judge_output_schema(output_kind=output_kind, allowed_decisions=allowed_decisions),
        }
        if self.judge_config.summary is not None:
            turn_params["summary"] = self.judge_config.summary
        if self.judge_config.effort is not None:
            turn_params["effort"] = self.judge_config.effort
        if self.judge_config.model is not None:
            turn_params["model"] = self.judge_config.model

        try:
            async with self._judge_call_lock:
                judge_client = await self._ensure_judge_client()
                # Each judge decision runs in a fresh ephemeral thread.
                response = await judge_client.responses_create(
                    prompt=prompt,
                    thread_params={"ephemeral": True},
                    turn_params=turn_params,
                )
        except Exception:
            await self.aclose()
            raise

        parsed = _parse_json_object(response.text)
        if not isinstance(parsed, dict):
            raise CodexAgenticError("LLM judge did not return a valid JSON object.")
        return parsed

    async def _ensure_judge_client(self) -> "AsyncCodexAgenticClient":
        existing = self._judge_client
        if existing is not None:
            return existing

        async with self._judge_client_lock:
            existing = self._judge_client
            if existing is not None:
                return existing

            from .factory import create_async_client

            judge_client = create_async_client(
                codex_command=self.codex_command,
                app_server_args=self.app_server_args,
                env=self.env,
                process_cwd=self.process_cwd,
                enable_web_search=self.judge_config.enable_web_search,
                server_config_overrides=self.server_config_overrides or None,
                default_turn_params={},
                default_thread_params={},
            )
            try:
                await judge_client.connect()
            except Exception:
                await judge_client.close()
                raise

            self._judge_client = judge_client
            return judge_client


def build_policy_engine_from_rubric(
    rubric: PolicyRubric | dict[str, Any],
    *,
    judge_config: PolicyJudgeConfig | None = None,
    codex_command: str = "codex",
    app_server_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    process_cwd: str | None = None,
    server_config_overrides: dict[str, Any] | None = None,
) -> PolicyEngine:
    normalized = _to_rubric(rubric)
    if normalized.use_llm_judge:
        return LlmRubricPolicyEngine(
            rubric=normalized,
            judge_config=judge_config,
            codex_command=codex_command,
            app_server_args=app_server_args,
            env=env,
            process_cwd=process_cwd,
            server_config_overrides=server_config_overrides,
        )
    return RuleBasedPolicyEngine(rubric=normalized)


def _to_rubric(rubric: PolicyRubric | dict[str, Any]) -> PolicyRubric:
    if isinstance(rubric, PolicyRubric):
        return rubric
    data = dict(rubric)
    return PolicyRubric(
        system_rubric=str(data.get("system_rubric") or DEFAULT_POLICY_RUBRIC.system_rubric),
        mode=str(data.get("mode") or "permissive"),  # type: ignore[arg-type]
        use_llm_judge=bool(data.get("use_llm_judge", False)),
        command_rules=[item for item in data.get("command_rules", []) if isinstance(item, dict)],
        file_change_rules=[item for item in data.get("file_change_rules", []) if isinstance(item, dict)],
        tool_input_rules=[item for item in data.get("tool_input_rules", []) if isinstance(item, dict)],
        defaults=data.get("defaults") if isinstance(data.get("defaults"), dict) else DEFAULT_POLICY_RUBRIC.defaults.copy(),
        audit=data.get("audit") if isinstance(data.get("audit"), dict) else DEFAULT_POLICY_RUBRIC.audit.copy(),
    )


def _matches_rule(
    when: dict[str, Any],
    params: dict[str, Any],
    context: PolicyContext,
    *,
    kind: RequestType,
) -> bool:
    common_keys = {
        "command_regex",
        "cwd_prefix",
        "reason_regex",
        "thread_id_regex",
        "turn_id_regex",
        "has_proposed_execpolicy",
    }
    kind_specific_keys: dict[RequestType, set[str]] = {
        "command": set(),
        "file_change": {"grant_root_present"},
        "tool_user_input": {"question_id_regex"},
    }
    allowed_keys = common_keys | kind_specific_keys[kind]
    unknown_keys = sorted(key for key in when if key not in allowed_keys)
    if unknown_keys:
        names = ", ".join(unknown_keys)
        raise CodexAgenticError(f"Unsupported policy rule fields for '{kind}': {names}")

    command = str(params.get("command") or "")
    cwd = str(params.get("cwd") or "")
    reason = str(params.get("reason") or "")
    proposed = params.get("proposedExecpolicyAmendment")
    has_proposed = isinstance(proposed, list) and len(proposed) > 0

    if "command_regex" in when:
        pattern = str(when["command_regex"])
        if not re.search(pattern, command):
            return False
    if "cwd_prefix" in when:
        prefix = str(when["cwd_prefix"])
        if not cwd.startswith(prefix):
            return False
    if "reason_regex" in when:
        pattern = str(when["reason_regex"])
        if not re.search(pattern, reason):
            return False
    if "thread_id_regex" in when:
        pattern = str(when["thread_id_regex"])
        if not re.search(pattern, context.thread_id or ""):
            return False
    if "turn_id_regex" in when:
        pattern = str(when["turn_id_regex"])
        if not re.search(pattern, context.turn_id or ""):
            return False
    if "has_proposed_execpolicy" in when:
        expected = bool(when["has_proposed_execpolicy"])
        if expected != has_proposed:
            return False
    if kind == "file_change" and "grant_root_present" in when:
        expected = bool(when["grant_root_present"])
        present = bool(params.get("grantRoot"))
        if expected != present:
            return False
    if kind == "tool_user_input" and "question_id_regex" in when:
        pattern = str(when["question_id_regex"])
        questions = params.get("questions")
        if not isinstance(questions, list):
            return False
        matched = False
        for question in questions:
            if not isinstance(question, dict):
                continue
            qid = question.get("id")
            if isinstance(qid, str) and re.search(pattern, qid):
                matched = True
                break
        if not matched:
            return False
    return True


def _normalize_command_or_file_decision(decision: Any, *, fallback: str) -> dict[str, Any]:
    valid = {"accept", "acceptForSession", "decline", "cancel"}
    if isinstance(decision, dict):
        payload = dict(decision)
        value = payload.get("decision")
        if isinstance(value, str) and value in valid:
            return payload
        if isinstance(value, dict) and "acceptWithExecpolicyAmendment" in value:
            return payload
        raise CodexAgenticError("Policy decision dict must contain a valid 'decision'.")
    if isinstance(decision, str):
        if decision == "auto_empty":
            return {"decision": fallback}
        if decision in valid:
            return {"decision": decision}
        raise CodexAgenticError("Policy decision string must be one of: accept, acceptForSession, decline, cancel.")
    raise CodexAgenticError("Policy decision must be a dict or string.")


def _normalize_user_input_decision(decision: Any) -> dict[str, Any]:
    if isinstance(decision, dict):
        answers = decision.get("answers")
        if isinstance(answers, list):
            mapped: dict[str, dict[str, list[str]]] = {}
            for row in answers:
                if not isinstance(row, dict):
                    continue
                qid = row.get("id")
                values = row.get("answers")
                if not isinstance(qid, str):
                    continue
                if not isinstance(values, list):
                    continue
                normalized_values = [value for value in values if isinstance(value, str)]
                mapped[qid] = {"answers": normalized_values}
            return {"answers": mapped}
        if isinstance(answers, dict):
            return {"answers": answers}
    return {"answers": {}}


def _build_judge_input(
    *,
    params: dict[str, Any],
    context: PolicyContext,
    include_params: bool,
    max_params_chars: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_type": context.request_type,
        "thread_id": context.thread_id,
        "turn_id": context.turn_id,
        "item_id": context.item_id,
    }
    if include_params:
        params_json = json.dumps(params, ensure_ascii=False)
        budget = max(0, int(max_params_chars))
        if len(params_json) > budget:
            if budget < 4:
                params_json = params_json[:budget]
            else:
                params_json = params_json[: budget - 3] + "..."
        payload["params_json"] = params_json
    return payload


def _build_judge_prompt(
    *,
    rubric: str,
    context: PolicyContext,
    allowed_decisions: list[str],
    input_payload: dict[str, Any],
    output_kind: Literal["command_or_file", "user_input"],
) -> str:
    if output_kind == "command_or_file":
        decision_text = ", ".join(allowed_decisions)
        return (
            "You are a policy judge.\n"
            f"Rubric:\n{rubric}\n\n"
            f"Request method: {context.method}\n"
            f"Allowed decisions: {decision_text}\n"
            "Return only JSON object: {\"decision\":\"...\",\"reason\":\"...\"}\n\n"
            f"Input:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}"
        )
    return (
        "You are a policy judge for requestUserInput.\n"
        f"Rubric:\n{rubric}\n\n"
        f"Request method: {context.method}\n"
        "Return only JSON object with shape: {\"answers\": [{\"id\":\"<question_id>\",\"answers\":[\"...\"]}]}\n"
        "If no confident answer, return {\"answers\": []}.\n\n"
        f"Input:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}"
    )


def _judge_output_schema(
    *,
    output_kind: Literal["command_or_file", "user_input"],
    allowed_decisions: list[str],
) -> dict[str, Any]:
    if output_kind == "command_or_file":
        return {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": allowed_decisions},
                "reason": {"type": "string"},
            },
            "required": ["decision", "reason"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "answers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "answers"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["answers"],
        "additionalProperties": False,
    }


def _parse_json_object(text: str) -> dict[str, Any] | None:
    value = first_nonempty_text(text)
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
