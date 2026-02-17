from __future__ import annotations

import asyncio
import shutil
from typing import Any

import pytest

from codex_python_sdk import (
    LlmRubricPolicyEngine,
    PolicyContext,
    PolicyJudgeConfig,
    RuleBasedPolicyEngine,
    build_policy_engine_from_rubric,
)
from codex_python_sdk.errors import AppServerConnectionError, CodexAgenticError, NotAuthenticatedError
from codex_python_sdk.policy import (
    DEFAULT_POLICY_RUBRIC,
    _build_judge_input,
    _matches_rule,
    _normalize_command_or_file_decision,
    _normalize_user_input_decision,
    _parse_json_object,
)


def _require_real_codex() -> None:
    if shutil.which("codex") is None:
        pytest.skip("`codex` command not found; skipping real integration tests.")


def _run_async_or_skip(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except NotAuthenticatedError as exc:
        pytest.skip(f"Codex not authenticated: {exc}")
    except AppServerConnectionError as exc:
        pytest.skip(f"Cannot connect to codex app-server: {exc}")
    except CodexAgenticError as exc:
        message = str(exc).lower()
        if "reconnecting" in message or "transport" in message or "stream ended" in message:
            pytest.skip(f"Transient codex runtime instability: {exc}")
        raise


def test_rule_based_policy_engine_matches_command_regex():
    engine = RuleBasedPolicyEngine(
        {
            "system_rubric": "test",
            "command_rules": [
                {
                    "name": "block_rm_root",
                    "priority": 5,
                    "when": {"command_regex": r"rm\s+-rf\s+/"},
                    "decision": "cancel",
                }
            ],
            "defaults": {"command": "accept", "file_change": "accept", "tool_input": "auto_empty"},
        }
    )
    context = PolicyContext(
        request_type="command",
        method="item/commandExecution/requestApproval",
        thread_id="t1",
        turn_id="u1",
        item_id="i1",
        params={"command": "rm -rf /"},
    )
    out = engine.on_command_approval({"command": "rm -rf /"}, context)
    assert out == {"decision": "cancel"}


def test_build_policy_engine_selects_expected_type():
    rb = build_policy_engine_from_rubric({"system_rubric": "test", "use_llm_judge": False})
    assert isinstance(rb, RuleBasedPolicyEngine)

    llm = build_policy_engine_from_rubric({"system_rubric": "test", "use_llm_judge": True})
    assert isinstance(llm, LlmRubricPolicyEngine)


def test_default_policy_rubric_is_not_shared_across_engines_or_global():
    rb1 = RuleBasedPolicyEngine()
    rb2 = RuleBasedPolicyEngine()
    assert rb1.rubric is not rb2.rubric
    assert rb1.rubric is not DEFAULT_POLICY_RUBRIC
    assert rb2.rubric is not DEFAULT_POLICY_RUBRIC

    rb1.rubric.command_rules.append({"name": "x", "when": {"command_regex": ".*"}, "decision": "decline"})
    assert rb2.rubric.command_rules == []
    assert DEFAULT_POLICY_RUBRIC.command_rules == []

    llm1 = LlmRubricPolicyEngine()
    llm2 = LlmRubricPolicyEngine()
    assert llm1.rubric is not llm2.rubric
    assert llm1.rubric is not DEFAULT_POLICY_RUBRIC
    assert llm2.rubric is not DEFAULT_POLICY_RUBRIC


def test_llm_policy_engine_init_does_not_create_locks_eagerly(monkeypatch):
    def _unexpected(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("asyncio.Lock should not be created during __init__")

    monkeypatch.setattr("codex_python_sdk.policy.asyncio.Lock", _unexpected)
    engine = LlmRubricPolicyEngine()
    assert engine._judge_client_lock_ref is None
    assert engine._judge_call_lock_ref is None


def test_llm_policy_engine_initializes_locks_lazily_in_running_loop():
    engine = LlmRubricPolicyEngine()

    async def _run() -> None:
        async with engine._judge_client_lock:
            pass
        async with engine._judge_call_lock:
            pass

    asyncio.run(_run())
    assert engine._judge_client_lock_ref is not None
    assert engine._judge_call_lock_ref is not None


def test_rule_based_policy_engine_raises_for_invalid_rule_decision():
    engine = RuleBasedPolicyEngine(
        {
            "system_rubric": "test",
            "command_rules": [
                {
                    "name": "bad_rule",
                    "priority": 1,
                    "when": {"command_regex": r"^rm"},
                    "decision": "typo-invalid",
                }
            ],
            "file_change_rules": [
                {
                    "name": "bad_file_rule",
                    "priority": 1,
                    "when": {"reason_regex": r".*"},
                    "decision": "typo-invalid",
                }
            ],
            "defaults": {"command": "decline", "file_change": "cancel", "tool_input": "auto_empty"},
        }
    )

    command_context = PolicyContext(
        request_type="command",
        method="item/commandExecution/requestApproval",
        thread_id="t1",
        turn_id="u1",
        item_id="i1",
        params={"command": "rm -rf /tmp/x"},
    )
    file_context = PolicyContext(
        request_type="file_change",
        method="item/fileChange/requestApproval",
        thread_id="t2",
        turn_id="u2",
        item_id="i2",
        params={"reason": "update file"},
    )

    try:
        engine.on_command_approval(command_context.params, command_context)
        raise AssertionError("expected CodexAgenticError")
    except CodexAgenticError as exc:
        assert "Policy decision string must be one of" in str(exc)

    try:
        engine.on_file_change_approval(file_context.params, file_context)
        raise AssertionError("expected CodexAgenticError")
    except CodexAgenticError as exc:
        assert "Policy decision string must be one of" in str(exc)


def test_user_input_normalization_from_list_shape():
    normalized = _normalize_user_input_decision(
        {
            "answers": [
                {"id": "q1", "answers": ["a1"]},
                {"id": "q2", "answers": ["a2", "a3"]},
            ]
        }
    )
    assert normalized == {
        "answers": {
            "q1": {"answers": ["a1"]},
            "q2": {"answers": ["a2", "a3"]},
        }
    }


def test_llm_policy_judge_fallback_preserves_cancellation(monkeypatch):
    engine = LlmRubricPolicyEngine(
        rubric={"system_rubric": "test", "use_llm_judge": True},
        judge_config=PolicyJudgeConfig(fallback_command_decision="decline"),
    )

    async def _raise_cancelled(
        *,
        params: dict[str, Any],
        context: PolicyContext,
        allowed_decisions: list[str],
        output_kind: str,
    ) -> dict[str, Any]:
        del params, context, allowed_decisions, output_kind
        raise asyncio.CancelledError

    monkeypatch.setattr(engine, "_judge_with_llm", _raise_cancelled)

    async def _run() -> None:
        context = PolicyContext(
            request_type="command",
            method="item/commandExecution/requestApproval",
            thread_id="cancel-thread",
            turn_id="cancel-turn",
            item_id="cancel-item",
            params={"command": "pwd"},
        )
        try:
            await engine.on_command_approval({"command": "pwd"}, context)
            raise AssertionError("expected CancelledError")
        except asyncio.CancelledError:
            return

    asyncio.run(_run())


def test_llm_policy_judge_fallback_on_regular_exception(monkeypatch):
    engine = LlmRubricPolicyEngine(
        rubric={"system_rubric": "test", "use_llm_judge": True},
        judge_config=PolicyJudgeConfig(fallback_command_decision="decline"),
    )

    async def _raise_runtime_error(
        *,
        params: dict[str, Any],
        context: PolicyContext,
        allowed_decisions: list[str],
        output_kind: str,
    ) -> dict[str, Any]:
        del params, context, allowed_decisions, output_kind
        raise RuntimeError("judge failed")

    monkeypatch.setattr(engine, "_judge_with_llm", _raise_runtime_error)

    async def _run() -> None:
        context = PolicyContext(
            request_type="command",
            method="item/commandExecution/requestApproval",
            thread_id="fallback-thread",
            turn_id="fallback-turn",
            item_id="fallback-item",
            params={"command": "pwd"},
        )
        try:
            await engine.on_command_approval({"command": "pwd"}, context)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert str(exc) == "judge failed"

    asyncio.run(_run())


def test_llm_policy_judge_fallback_on_malformed_command_payload(monkeypatch):
    engine = LlmRubricPolicyEngine(
        rubric={"system_rubric": "test", "use_llm_judge": True},
        judge_config=PolicyJudgeConfig(fallback_command_decision="decline"),
    )

    async def _malformed_payload(
        *,
        params: dict[str, Any],
        context: PolicyContext,
        allowed_decisions: list[str],
        output_kind: str,
    ) -> dict[str, Any]:
        del params, context, allowed_decisions, output_kind
        return {"decision": "deny"}

    monkeypatch.setattr(engine, "_judge_with_llm", _malformed_payload)

    async def _run() -> None:
        context = PolicyContext(
            request_type="command",
            method="item/commandExecution/requestApproval",
            thread_id="malformed-thread",
            turn_id="malformed-turn",
            item_id="malformed-item",
            params={"command": "pwd"},
        )
        try:
            await engine.on_command_approval({"command": "pwd"}, context)
            raise AssertionError("expected CodexAgenticError")
        except CodexAgenticError as exc:
            assert "Policy decision dict must contain a valid 'decision'" in str(exc)

    asyncio.run(_run())


def test_llm_policy_judge_omits_none_summary_from_turn_params(monkeypatch):
    engine = LlmRubricPolicyEngine(
        rubric={"system_rubric": "test", "use_llm_judge": True},
        judge_config=PolicyJudgeConfig(summary=None, effort=None, model=None),
    )
    captured: dict[str, Any] = {}

    class _FakeJudgeResponse:
        text = '{"decision":"accept","reason":"ok"}'

    class _FakeJudgeClient:
        async def responses_create(self, *, prompt: str, thread_params: dict[str, Any], turn_params: dict[str, Any]):
            captured["prompt"] = prompt
            captured["thread_params"] = thread_params
            captured["turn_params"] = turn_params
            return _FakeJudgeResponse()

    async def _fake_ensure_judge_client():
        return _FakeJudgeClient()

    monkeypatch.setattr(engine, "_ensure_judge_client", _fake_ensure_judge_client)

    async def _run() -> dict[str, Any]:
        context = PolicyContext(
            request_type="command",
            method="item/commandExecution/requestApproval",
            thread_id="thread-1",
            turn_id="turn-1",
            item_id="item-1",
            params={"command": "pwd"},
        )
        return await engine._judge_with_llm(
            params={"command": "pwd"},
            context=context,
            allowed_decisions=["accept", "decline"],
            output_kind="command_or_file",
        )

    out = asyncio.run(_run())
    assert out == {"decision": "accept", "reason": "ok"}
    assert captured["thread_params"] == {"ephemeral": True}
    assert "summary" not in captured["turn_params"]


def test_matches_rule_honors_command_context_conditions():
    context = PolicyContext(
        request_type="command",
        method="item/commandExecution/requestApproval",
        thread_id="thread-123",
        turn_id="turn-abc",
        item_id="item-1",
        params={},
    )
    params = {
        "command": "rm -rf /tmp/cache",
        "cwd": "/tmp/workspace",
        "reason": "cleanup",
        "proposedExecpolicyAmendment": [{"path": "/tmp/workspace"}],
    }
    when = {
        "command_regex": r"rm\s+-rf",
        "cwd_prefix": "/tmp",
        "reason_regex": "clean",
        "thread_id_regex": "thread-\\d+",
        "turn_id_regex": "turn-",
        "has_proposed_execpolicy": True,
    }
    assert _matches_rule(when, params, context, kind="command") is True

    unmatched = dict(when)
    unmatched["has_proposed_execpolicy"] = False
    assert _matches_rule(unmatched, params, context, kind="command") is False


def test_matches_rule_tool_input_and_file_change_specific_conditions():
    tool_context = PolicyContext(
        request_type="tool_user_input",
        method="item/tool/requestUserInput",
        thread_id="thread-1",
        turn_id="turn-1",
        item_id="item-1",
        params={},
    )
    tool_params = {"questions": [{"id": "q-approval", "question": "Proceed?"}]}
    assert _matches_rule({"question_id_regex": r"q-approval"}, tool_params, tool_context, kind="tool_user_input") is True
    assert _matches_rule({"question_id_regex": r"q-missing"}, tool_params, tool_context, kind="tool_user_input") is False

    file_context = PolicyContext(
        request_type="file_change",
        method="item/fileChange/requestApproval",
        thread_id="thread-2",
        turn_id="turn-2",
        item_id="item-2",
        params={},
    )
    file_params = {"grantRoot": "/repo"}
    assert _matches_rule({"grant_root_present": True}, file_params, file_context, kind="file_change") is True
    assert _matches_rule({"grant_root_present": False}, file_params, file_context, kind="file_change") is False


def test_matches_rule_rejects_unknown_rule_fields():
    context = PolicyContext(
        request_type="command",
        method="item/commandExecution/requestApproval",
        thread_id="thread-1",
        turn_id="turn-1",
        item_id="item-1",
        params={},
    )
    try:
        _matches_rule({"unknown_key": "x"}, {"command": "pwd"}, context, kind="command")
        raise AssertionError("expected CodexAgenticError")
    except CodexAgenticError as exc:
        assert "Unsupported policy rule fields for 'command': unknown_key" in str(exc)


def test_normalize_command_or_file_decision_rejects_invalid_dict():
    try:
        _normalize_command_or_file_decision({"decision": "deny"}, fallback="accept")
        raise AssertionError("expected CodexAgenticError")
    except CodexAgenticError as exc:
        assert "valid 'decision'" in str(exc)


def test_parse_json_object_requires_strict_json_object_and_handles_invalid_text():
    assert _parse_json_object('{"decision":"accept","reason":"ok"}') == {
        "decision": "accept",
        "reason": "ok",
    }
    assert _parse_json_object('prefix {"decision":"accept","reason":"ok"} suffix') is None
    assert _parse_json_object("no json here") is None


def test_build_judge_input_respects_tiny_max_params_chars():
    context = PolicyContext(
        request_type="command",
        method="item/commandExecution/requestApproval",
        thread_id="thread-1",
        turn_id="turn-1",
        item_id="item-1",
        params={},
    )
    params = {"command": "echo", "long": "x" * 100}

    for budget in (0, 1, 2, 3):
        payload = _build_judge_input(
            params=params,
            context=context,
            include_params=True,
            max_params_chars=budget,
        )
        params_json = payload.get("params_json")
        assert isinstance(params_json, str)
        assert len(params_json) <= budget

    payload = _build_judge_input(
        params=params,
        context=context,
        include_params=True,
        max_params_chars=5,
    )
    params_json = payload.get("params_json")
    assert isinstance(params_json, str)
    assert len(params_json) <= 5
    assert params_json.endswith("...")


@pytest.mark.real
def test_llm_policy_command_file_user_input_real():
    _require_real_codex()

    engine = LlmRubricPolicyEngine(
        rubric={
            "system_rubric": (
                "For this test, prefer decision='accept' for command and file approvals, "
                "and provide a short reason. "
                "For requestUserInput, choose the first option when available."
            ),
            "use_llm_judge": True,
        },
        judge_config=PolicyJudgeConfig(
            timeout_seconds=30.0,
            model="gpt-5",
            effort="low",
            fallback_command_decision="cancel",
            fallback_file_change_decision="cancel",
        ),
    )

    async def _run() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        command_ctx = PolicyContext(
            request_type="command",
            method="item/commandExecution/requestApproval",
            thread_id="it-cmd",
            turn_id="it-cmd",
            item_id="it-cmd",
            params={"command": "pwd", "cwd": "/tmp", "reason": "integration"},
        )
        file_ctx = PolicyContext(
            request_type="file_change",
            method="item/fileChange/requestApproval",
            thread_id="it-file",
            turn_id="it-file",
            item_id="it-file",
            params={"change": {"path": "README.md", "kind": "modify"}, "reason": "integration"},
        )
        tool_ctx = PolicyContext(
            request_type="tool_user_input",
            method="item/tool/requestUserInput",
            thread_id="it-tool",
            turn_id="it-tool",
            item_id="it-tool",
            params={
                "questions": [
                    {
                        "id": "approval",
                        "question": "Proceed?",
                        "options": [
                            {"label": "yes", "description": "continue"},
                            {"label": "no", "description": "stop"},
                        ],
                    }
                ]
            },
        )

        command_decision = await engine.on_command_approval(command_ctx.params, command_ctx)
        file_decision = await engine.on_file_change_approval(file_ctx.params, file_ctx)
        tool_decision = await engine.on_tool_request_user_input(tool_ctx.params, tool_ctx)
        raw_tool_judge = await engine._judge_with_llm(
            params=tool_ctx.params,
            context=tool_ctx,
            allowed_decisions=[],
            output_kind="user_input",
        )
        await engine.aclose()
        return command_decision, file_decision, tool_decision, raw_tool_judge

    command_decision, file_decision, tool_decision, raw_tool_judge = _run_async_or_skip(_run())
    valid = {"accept", "acceptForSession", "decline", "cancel"}
    assert command_decision.get("decision") in valid
    assert file_decision.get("decision") in valid
    assert isinstance(raw_tool_judge, dict)
    assert "answers" in raw_tool_judge
    assert isinstance(tool_decision, dict)
    assert isinstance(tool_decision.get("answers"), dict)


@pytest.mark.real
def test_llm_policy_reuses_single_judge_client_real():
    _require_real_codex()

    engine = LlmRubricPolicyEngine(
        rubric={
            "system_rubric": "Prefer accept for safe readonly commands and include reason.",
            "use_llm_judge": True,
        },
        judge_config=PolicyJudgeConfig(
            timeout_seconds=30.0,
            model="gpt-5",
            effort="low",
            fallback_command_decision="cancel",
        ),
    )

    async def _run() -> tuple[dict[str, Any], dict[str, Any], int, int]:
        context = PolicyContext(
            request_type="command",
            method="item/commandExecution/requestApproval",
            thread_id="reuse-thread",
            turn_id="reuse-turn",
            item_id="reuse-item",
            params={"command": "pwd", "cwd": "/tmp", "reason": "reuse"},
        )
        out1 = await engine.on_command_approval(context.params, context)
        client_id_1 = id(engine._judge_client)
        out2 = await engine.on_command_approval({"command": "ls", "cwd": "/tmp", "reason": "reuse"}, context)
        client_id_2 = id(engine._judge_client)
        await engine.aclose()
        return out1, out2, client_id_1, client_id_2

    out1, out2, client_id_1, client_id_2 = _run_async_or_skip(_run())
    valid = {"accept", "acceptForSession", "decline", "cancel"}
    assert out1.get("decision") in valid
    assert out2.get("decision") in valid
    assert client_id_1 == client_id_2


@pytest.mark.real
def test_llm_policy_fallback_on_invalid_model_real():
    _require_real_codex()

    engine = LlmRubricPolicyEngine(
        rubric={"system_rubric": "always accept", "use_llm_judge": True},
        judge_config=PolicyJudgeConfig(
            timeout_seconds=8.0,
            model="definitely-not-a-real-model",
            effort="low",
            fallback_command_decision="decline",
        ),
    )

    async def _run() -> dict[str, Any]:
        context = PolicyContext(
            request_type="command",
            method="item/commandExecution/requestApproval",
            thread_id="fb-thread",
            turn_id="fb-turn",
            item_id="fb-item",
            params={"command": "pwd"},
        )
        out = await engine.on_command_approval({"command": "pwd"}, context)
        await engine.aclose()
        return out

    out = _run_async_or_skip(_run())
    assert out == {"decision": "decline"}
