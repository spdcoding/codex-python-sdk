# codex-python-sdk

[GitHub](https://github.com/spdcoding/codex-python-sdk) | [English](./README.md) | [简体中文](./README.zh-CN.md) | [Docs](./docs)

面向生产环境的 Python SDK，用于通过 `codex app-server` 运行 Codex 智能体。

`codex-python-sdk` 基于 Codex JSON-RPC 提供稳定接口，帮助你在应用中自动化编排 agent 工作流、流式消费结构化事件，并对运行时行为做策略控制。

## 为什么选择这个 SDK

- 脚本优先：面向自动化流水线，而不仅是交互式 CLI。
- 同步/异步一致：两套客户端心智模型统一，方法命名基本一致。
- 结构化流式输出：使用标准化 `ResponseEvent` 做可观测性和自定义 UI。
- 错误语义清晰：`NotAuthenticatedError`、`SessionNotFoundError` 等可预测异常。
- 策略可控：支持审批、文件变更、工具输入、工具调用钩子与策略引擎。
- 协议封装克制：尽量贴近 `codex app-server` 原生行为，便于定位问题。

## 30 秒快速开始

```python
from codex_python_sdk import create_client

with create_client() as client:
    result = client.responses_create(prompt="Reply with exactly: READY")
    print(result.session_id)
    print(result.text)
```

## 核心工作流

### 流式事件（日志/UI）

```python
from codex_python_sdk import create_client, render_exec_style_events

with create_client() as client:
    events = client.responses_events(prompt="Summarize this repository")
    render_exec_style_events(events)
```

### 异步调用

```python
import asyncio
from codex_python_sdk import create_async_client


async def main() -> None:
    async with create_async_client() as client:
        result = await client.responses_create(prompt="Reply with exactly: ASYNC_READY")
        print(result.text)


asyncio.run(main())
```

### Smoke 与 Demo Runner

```bash
# 快速健康检查（默认模式）
codex-python-sdk-demo --mode smoke

# 稳定 API 演示
codex-python-sdk-demo --mode demo

# Demo + 不稳定的 remote/interrupt/compact 路径
codex-python-sdk-demo --mode full
```

说明：demo runner 为了无人值守，默认使用宽松钩子（命令/文件审批 `accept`，工具输入为空）。
生产环境建议替换为更严格的钩子或策略引擎。

## 核心机制（心智模型）

`codex app-server` 是 Codex CLI 在本地通过 stdio 暴露的 JSON-RPC 运行时。

一次 `responses_create(prompt=...)` 本质上是：

1. `create_client()` 创建同步外观层（`CodexAgenticClient`）。
2. 同步调用通过独立 event-loop 线程转发到 `AsyncCodexAgenticClient`。
3. `connect()` 拉起 `codex app-server` 并完成 `initialize/initialized` 握手。
4. `_request(method, params)` 统一处理 JSON-RPC 请求/响应。
5. `responses_events()` 负责通知流；`responses_create()` 负责聚合最终文本。

如果你想深入理解，建议看独立架构文档 `docs/zh/core_mechanism.md`。

## 默认安全行为（重要）

在不传钩子/策略时，默认行为：
- 命令审批：`accept`
- 文件变更审批：`accept`
- 工具用户输入：空答案
- 工具调用：返回带说明文本的失败响应

这对无人值守演示友好，但不适合作为生产默认安全姿态。

建议使用 LLM judge，并将兜底决策设置为拒绝：

```python
from codex_python_sdk import PolicyJudgeConfig, create_client

rubric = {
    "system_rubric": "允许只读操作；对未知写操作默认拒绝。",
    "use_llm_judge": True,
}

judge_cfg = PolicyJudgeConfig(
    timeout_seconds=8.0,
    model="gpt-5",
    effort="low",
    fallback_command_decision="decline",
    fallback_file_change_decision="decline",
)

with create_client(
    policy_rubric=rubric,
    policy_judge_config=judge_cfg,
) as client:
    result = client.responses_create(prompt="Show git status.")
    print(result.text)
```

说明：LLM judge 依赖真实 Codex 运行时/账号；如果是本地可重复测试，建议使用 `RuleBasedPolicyEngine`。

## 安装

### 前置要求

- Python `3.9+`（推荐 `3.12`）
- `uv`（推荐用于开发工作流）
- 已安装且可运行的 `codex` CLI
- 已通过 `codex login` 完成认证

### 从 PyPI 安装

```bash
uv add codex-python-sdk
uv run codex-python-sdk-demo --help
```

或

```bash
pip install codex-python-sdk
codex-python-sdk-demo --help
```

### 开发环境安装（贡献者）

```bash
./uv-sync.sh
```

该命令会初始化本地 `.venv`，并安装项目本体及测试/构建依赖。

## API 一览

工厂函数：
- `create_client(**kwargs) -> CodexAgenticClient`
- `create_async_client(**kwargs) -> AsyncCodexAgenticClient`

高频响应 API：
- `responses_create(...) -> AgentResponse`
- `responses_events(...) -> Iterator[ResponseEvent] / AsyncIterator[ResponseEvent]`
- `responses_stream_text(...) -> Iterator[str] / AsyncIterator[str]`

Thread 基础 API：
- `thread_start`、`thread_read`、`thread_list`、`thread_archive`

账户基础 API：
- `account_read`、`account_rate_limits_read`

## 文档导航

中文文档：
- `docs/zh/tutorial.md`：实用工作流与端到端用法
- `docs/zh/core_mechanism.md`：核心调用链与执行机制
- `docs/zh/config.md`：server/thread/turn 配置模型
- `docs/zh/api.md`：完整 API 参考（sync + async）
- `docs/zh/policy.md`：审批钩子与策略引擎
- `docs/zh/app_server.md`：app-server 概念与协议映射

English docs:
- `docs/tutorial.md`
- `docs/core_mechanism.md`
- `docs/config.md`
- `docs/api.md`
- `docs/policy.md`
- `docs/app_server.md`

## 注意事项

- 遇到 `AppServerConnectionError` 时请重建 client，不要依赖隐式自动重连。
- 内部 app-server `stderr` 缓冲在 SDK 诊断上下文中仅保留最近 500 行。
- 使用底层 server request handlers 时，method 名必须精确为 `item`、`tool` 或 `requestUserInput`。
- Policy 的 LLM judge 采用严格 JSON-only 解析：judge 输出必须是纯 JSON 对象；自由文本中的嵌入 JSON 不会被提取。
- command/file policy decision 的非法取值（合法值：`accept`、`acceptForSession`、`decline`、`cancel`）会抛出 `CodexAgenticError`。

## 开发

```bash
./uv-sync.sh
uv run python3 -m pytest -q -m "not real"
```

## 发布

```bash
# 默认：测试 + 构建 + twine check（不上传）
./build.sh

# 仅构建
./build.sh build

# 发布到 pypi（显式开启上传）
TWINE_UPLOAD=1 ./build.sh release --repo pypi

# 发布到 testpypi
TWINE_UPLOAD=1 ./build.sh release --repo testpypi

# 仅上传已有产物
./build.sh upload --repo pypi

# 帮助
./build.sh help
```

推荐使用配置了 API token 的 `~/.pypirc` 完成上传认证。

## 项目结构

- `codex_python_sdk/`：SDK 源代码
- `codex_python_sdk/examples/`：可运行 demo 代码
- `tests/`：单元测试与真实运行时集成测试
- `uv-sync.sh`：开发环境初始化
- `build.sh`：构建/发布脚本

## 错误类型

- `CodexAgenticError`：SDK 基础错误
- `AppServerConnectionError`：app-server 传输/初始化失败
- `SessionNotFoundError`：未知 thread/session id
- `NotAuthenticatedError`：认证不可用或无效
