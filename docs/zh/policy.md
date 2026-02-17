# 策略与审批钩子

[English](../policy.md) | [简体中文](./policy.md)

`codex-python-sdk` 通过显式钩子与策略引擎支持基于策略的审批与工具行为控制。

## 1. 钩子优先级

解析顺序：
1. 显式钩子（`on_*`）
2. `policy_engine`
3. `policy_rubric` 自动构建的引擎
4. 内置默认行为

## 2. 可用钩子

- `on_command_approval(params)`
- `on_file_change_approval(params)`
- `on_tool_request_user_input(params)`
- `on_tool_call(params)`

这些钩子作用于客户端实例。输入负载在可用时会包含 `threadId`、`turnId`、`itemId` 等标识。

## 3. 命令审批

常见请求字段：
- `threadId`、`turnId`、`itemId`
- `command`、`cwd`、`reason`
- `commandActions`

常见决策：
- `accept`
- `acceptForSession`
- `decline`
- `cancel`
- `acceptWithExecpolicyAmendment`

## 4. 文件变更审批

常见请求字段：
- `threadId`、`turnId`、`itemId`
- `reason`、`grantRoot`

常见决策：
- `accept`
- `acceptForSession`
- `decline`
- `cancel`

## 5. 用户输入钩子

用于 `item/tool/requestUserInput` 与 `tool/requestUserInput`。

返回结构：

```python
{
    "answers": {
        "<question_id>": {
            "answers": ["..."]
        }
    }
}
```

## 6. 工具调用钩子

用于 `item/tool/call`。

返回结构（`DynamicToolCallResponse`）：

```python
{
    "success": True,
    "contentItems": [
        {"type": "inputText", "text": "..."}
    ]
}
```

## 7. 规则策略引擎

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

## 8. LLM-Judge Rubric 模式

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

如果 judge 失败或超时，会应用回退决策。

## 9. 默认行为（无钩子 / 无策略）

- 命令审批：accept
- 文件变更审批：accept
- 工具用户输入：空答案
- 工具调用：返回带说明文本的失败响应

这也是为什么 `codex-python-sdk-demo` 可以在无交互审批下运行。生产环境不要依赖这些默认值。
