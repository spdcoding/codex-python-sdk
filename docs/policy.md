# Policy and Approval Hooks

[English](./policy.md) | [简体中文](./zh/policy.md)

`codex-python-sdk` supports policy-driven approval and tool behaviors through explicit hooks and policy engines.

## 1. Hook Priority

Resolution order:
1. Explicit hook (`on_*`)
2. `policy_engine`
3. `policy_rubric` auto-built engine
4. Built-in default behavior

## 2. Available Hooks

- `on_command_approval(params)`
- `on_file_change_approval(params)`
- `on_tool_request_user_input(params)`
- `on_tool_call(params)`

These hooks are client-instance scoped. Input payloads include identifiers like `threadId`, `turnId`, and `itemId` when available.

## 3. Command Approval

Typical request fields:
- `threadId`, `turnId`, `itemId`
- `command`, `cwd`, `reason`
- `commandActions`

Typical decisions:
- `accept`
- `acceptForSession`
- `decline`
- `cancel`
- `acceptWithExecpolicyAmendment`

## 4. File Change Approval

Typical request fields:
- `threadId`, `turnId`, `itemId`
- `reason`, `grantRoot`

Typical decisions:
- `accept`
- `acceptForSession`
- `decline`
- `cancel`

## 5. User Input Hook

Used for `item/tool/requestUserInput` and `tool/requestUserInput`.

Return shape:

```python
{
    "answers": {
        "<question_id>": {
            "answers": ["..."]
        }
    }
}
```

## 6. Tool Call Hook

Used for `item/tool/call`.

Return shape (`DynamicToolCallResponse`):

```python
{
    "success": True,
    "contentItems": [
        {"type": "inputText", "text": "..."}
    ]
}
```

## 7. Rule-Based Policy Engine

```python
from codex_python_sdk import PolicyRubric, RuleBasedPolicyEngine, create_client

rubric = PolicyRubric(
    system_rubric="Prefer safe behavior for destructive commands.",
    command_rules=[
        {
            "name": "block_dangerous_rm",
            "priority": 10,
            "when": {"command_regex": r"rm\\s+-rf\\s+/"},
            "decision": "cancel",
        }
    ],
    defaults={"command": "accept", "file_change": "accept", "tool_input": "auto_empty"},
)

engine = RuleBasedPolicyEngine(rubric)

with create_client(policy_engine=engine) as client:
    pass
```

## 8. LLM-Judge Rubric Mode

```python
from codex_python_sdk import PolicyJudgeConfig, create_client

rubric = {
    "system_rubric": "Approve read-only commands. Decline unknown write commands.",
    "use_llm_judge": True,
}

judge_cfg = PolicyJudgeConfig(
    timeout_seconds=8.0,
    effort="medium",
    fallback_command_decision="decline",
    fallback_file_change_decision="decline",
)

with create_client(policy_rubric=rubric, policy_judge_config=judge_cfg) as client:
    pass
```

If judge fails or times out, fallback decisions are applied.

## 9. Default Behavior (No Hooks / No Policy)

- Command approval: accept
- File change approval: accept
- Tool user input: empty answers
- Tool call: failure response with explanatory text

This is also why `codex-python-sdk-demo` can run without interactive approvals. Do not rely on these defaults for production safety.
