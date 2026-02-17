# 教程

[English](../tutorial.md) | [简体中文](./tutorial.md)

本教程介绍 `codex-python-sdk` 的实用使用模式。

## 1. 前置要求

- Python `3.9+`（推荐 `3.12`）
- `uv`（推荐用于开发工作流）
- `codex` CLI
- 已通过 `codex login` 完成本地认证

安装：

```bash
uv add codex-python-sdk
```

## 2. 最小请求/响应

```python
from codex_python_sdk import create_client

with create_client() as client:
    result = client.responses_create(prompt="Reply with exactly: READY")
    print(result.session_id)
    print(result.text)
```

当 `include_events=True` 时，返回的 `AgentResponse` 还会包含收集到的全部 `ResponseEvent` 条目。

## 3. 核心机制速览（从 `responses_create` 到 JSON-RPC）

这个 SDK 的核心可以压缩成一句话：`sync` 只是包装层，真正逻辑都在
`AsyncCodexAgenticClient`，通过 `stdio + JSON-RPC` 驱动 `codex app-server`。

一次 `responses_create(prompt=...)` 的主链路：

1. 入口工厂  
   `create_client()` 返回 `CodexAgenticClient`（同步外观层）。
2. 同步转异步  
   同步方法通过内部 event loop 线程，转发到异步 client。
3. 连接与初始化  
   `connect()` 启动 `codex app-server` 子进程，发送 `initialize/initialized` 握手。
4. 统一请求底座  
   所有 RPC 都走 `_request(method, params)`：分配请求 id、发送 JSON、等待对应响应 future。
5. 响应 API 组合  
   `responses_events()` 负责 `thread/start|resume -> turn/start -> 持续消费 notification`；  
   `responses_create()` 只是对事件流做聚合，产出最终 `AgentResponse.text`。

最小骨架（简化版）：

```python
async def responses_create(prompt: str, session_id: str | None = None) -> str:
    chunks = []
    async for event in responses_events(prompt=prompt, session_id=session_id):
        if event.text_delta:
            chunks.append(event.text_delta)
    return "".join(chunks).strip()

async def responses_events(prompt: str, session_id: str | None = None):
    thread_id = await ensure_thread(session_id)              # thread/start or thread/resume
    await _request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]})
    while True:
        note = await wait_notification(thread_id)            # 来自 app-server 的流式通知
        event = normalize(note)                              # -> ResponseEvent
        yield event
        if event.type == "turn/completed":
            break
```

建议对照源码阅读（核心文件）：
- `codex_python_sdk/factory.py`
- `codex_python_sdk/sync_client.py`
- `codex_python_sdk/async_client.py`
- `codex_python_sdk/types.py`

如果要看完整版架构说明，请继续阅读 `./core_mechanism.md`。

## 4. 在多轮中复用 Session

```python
from codex_python_sdk import create_client

with create_client() as client:
    first = client.responses_create(prompt="Reply with exactly TURN1")
    second = client.responses_create(
        prompt="Reply with exactly TURN2",
        session_id=first.session_id,
    )
    print(first.text, second.text)
```

如果 `thread/resume` 返回 `no rollout found` / `rollout not found`，SDK 会回退到原始
`session_id`，并继续执行 `turn/start`。

## 5. 流式消费结构化事件

```python
from codex_python_sdk import create_client

with create_client() as client:
    for event in client.responses_events(prompt="Summarize this repository"):
        print(event.type, event.phase, event.message_text or event.text_delta)
```

常用事件字段：
- `type`
- `phase`
- `session_id`
- `turn_id`
- `item_id`
- `text_delta` / `message_text`

## 6. 渲染 Exec 风格日志

```python
from codex_python_sdk import create_client, render_exec_style_events

with create_client() as client:
    events = client.responses_events(prompt="Analyze current project structure")
    render_exec_style_events(
        events,
        show_reasoning=True,
        show_system=False,
        show_tool_output=True,
        color="auto",
    )
```

## 7. 异步用法

```python
import asyncio
from codex_python_sdk import create_async_client


async def main() -> None:
    async with create_async_client() as client:
        result = await client.responses_create(prompt="Reply with exactly ASYNC_READY")
        print(result.session_id)
        print(result.text)


asyncio.run(main())
```

## 8. 结构化 JSON 输出

```python
from codex_python_sdk import create_client

with create_client() as client:
    result = client.responses_create(
        prompt="Return JSON with keys title and items",
        turn_params={
            "outputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "items"],
                "additionalProperties": False,
            }
        },
    )
    print(result.text)
```

## 9. 认证失败

如果认证不可用或已过期，API 会抛出 `NotAuthenticatedError`。

推荐处理方式：

```python
from codex_python_sdk import NotAuthenticatedError, create_client

try:
    with create_client() as client:
        client.responses_create(prompt="Hello")
except NotAuthenticatedError:
    print("Run `codex login` and retry.")
```

## 10. 下一步可读指南

- `./config.md`：配置模型与模板
- `./core_mechanism.md`：核心调用链与执行机制（架构向）
- `./api.md`：完整方法列表
- `./policy.md`：策略钩子与策略引擎
- `./app_server.md`：app-server 概念模型与 JSON-RPC 协议

## 11. 内置 Smoke/Demo CLI

包内包含 `codex-python-sdk-demo`，提供三种模式：

```bash
codex-python-sdk-demo --mode smoke  # 快速健康检查
codex-python-sdk-demo --mode demo   # 稳定 API 演示
codex-python-sdk-demo --mode full   # 演示 + 不稳定的 remote/interrupt/compact 路径
```

demo runner 为了无人值守执行，默认使用宽松审批策略：
- 命令审批返回 `accept`
- 文件变更审批返回 `accept`
- 工具用户输入请求返回空答案

在真实部署中请使用更严格的钩子或策略引擎。
`--mode full` 还依赖远端接口，可能因上游/服务端瞬时错误而失败。
