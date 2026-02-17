# codex-python-sdk

[GitHub](https://github.com/spdcoding/codex-python-sdk) | [English](./README.md) | [简体中文](./README.zh-CN.md) | [Docs](./docs)

Production-focused Python SDK for running Codex agents through `codex app-server`.

`codex-python-sdk` gives you a stable Python interface over Codex JSON-RPC so you can automate agent workflows, stream structured runtime events, and enforce runtime policy from your own applications.

## Why This SDK

- Script-first API: built for automation pipelines, not only interactive CLI sessions.
- Sync + async parity: same mental model and similar method names in both clients.
- Structured streaming: consume normalized `ResponseEvent` objects for observability and UI.
- Predictable failures: explicit error types such as `NotAuthenticatedError` and `SessionNotFoundError`.
- Policy control: approval/file-change/tool-input/tool-call hooks and policy engine integration.
- Thin protocol wrapper: close to `codex app-server` behavior, easier to reason about and debug.

## 30-Second Quick Start

```python
from codex_python_sdk import create_client

with create_client() as client:
    result = client.responses_create(prompt="Reply with exactly: READY")
    print(result.session_id)
    print(result.text)
```

## Core Workflows

### Stream events (for logs/UI)

```python
from codex_python_sdk import create_client, render_exec_style_events

with create_client() as client:
    events = client.responses_events(prompt="Summarize this repository")
    render_exec_style_events(events)
```

### Async flow

```python
import asyncio
from codex_python_sdk import create_async_client


async def main() -> None:
    async with create_async_client() as client:
        result = await client.responses_create(prompt="Reply with exactly: ASYNC_READY")
        print(result.text)


asyncio.run(main())
```

### Smoke and demo runner

```bash
# Quick health check (default mode)
codex-python-sdk-demo --mode smoke

# Stable API showcase
codex-python-sdk-demo --mode demo

# Demo + unstable remote/interrupt/compact paths
codex-python-sdk-demo --mode full
```

Note: the demo runner uses permissive hooks (`accept` for command/file approvals and empty tool-input answers) so it can run unattended.
Use stricter hooks or policy engines in production.

## Mental Model: How It Works

`codex app-server` is Codex CLI's local JSON-RPC runtime over stdio.

One `responses_create(prompt=...)` call is essentially:

1. `create_client()` creates a sync facade (`CodexAgenticClient`).
2. Sync call forwards to `AsyncCodexAgenticClient` via a dedicated event-loop thread.
3. `connect()` starts `codex app-server` and performs `initialize/initialized`.
4. `_request(method, params)` handles all JSON-RPC request/response plumbing.
5. `responses_events()` streams notifications; `responses_create()` aggregates them into final text.

For a deeper walkthrough, see `docs/core_mechanism.md`.

## Safety Defaults (Important)

Default behavior without hooks/policy:
- Command approval: `accept`
- File change approval: `accept`
- Tool user input: empty answers
- Tool call: failure response with explanatory text

This is convenient for unattended demos, but not production-safe.

Recommended safer setup: enable LLM-judge policy with strict fallback decisions.

```python
from codex_python_sdk import PolicyJudgeConfig, create_client

rubric = {
    "system_rubric": "Allow read-only operations. Decline unknown write operations.",
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

Note: LLM-judge requires a real Codex runtime/account; for deterministic local tests, use `RuleBasedPolicyEngine`.

## Install

### Prerequisites

- Python `3.9+` (recommended: `3.12`)
- `uv` (recommended for development workflows)
- `codex` CLI installed and runnable
- Authentication completed via `codex login`

### Install from PyPI

```bash
uv add codex-python-sdk
uv run codex-python-sdk-demo --help
```

or

```bash
pip install codex-python-sdk
codex-python-sdk-demo --help
```

### Developer setup (for contributors)

```bash
./uv-sync.sh
```

This bootstraps a local `.venv` and installs project/test/build dependencies.

## API Snapshot

Factory:
- `create_client(**kwargs) -> CodexAgenticClient`
- `create_async_client(**kwargs) -> AsyncCodexAgenticClient`

High-frequency response APIs:
- `responses_create(...) -> AgentResponse`
- `responses_events(...) -> Iterator[ResponseEvent] / AsyncIterator[ResponseEvent]`
- `responses_stream_text(...) -> Iterator[str] / AsyncIterator[str]`

Thread basics:
- `thread_start`, `thread_read`, `thread_list`, `thread_archive`

Account basics:
- `account_read`, `account_rate_limits_read`

## Documentation Map

English:
- `docs/tutorial.md`: practical workflows and end-to-end usage
- `docs/core_mechanism.md`: architecture-level core control flow
- `docs/config.md`: server/thread/turn configuration model
- `docs/api.md`: full API reference (sync + async)
- `docs/policy.md`: hooks and policy engine integration
- `docs/app_server.md`: app-server concepts and protocol mapping

简体中文:
- `docs/zh/tutorial.md`
- `docs/zh/core_mechanism.md`
- `docs/zh/config.md`
- `docs/zh/api.md`
- `docs/zh/policy.md`
- `docs/zh/app_server.md`

## Notes

- After `AppServerConnectionError`, recreate the client instead of relying on implicit reconnect behavior.
- Internal app-server `stderr` buffering keeps only the latest 500 lines in SDK-captured diagnostics.
- When using low-level server request handlers, method names must be exactly `item`, `tool`, or `requestUserInput`.
- Policy LLM-judge parsing is strict JSON-only: judge output must be a pure JSON object; embedded JSON snippets in free text are rejected.
- Invalid command/file policy decision values (allowed: `accept`, `acceptForSession`, `decline`, `cancel`) raise `CodexAgenticError`.

## Development

```bash
./uv-sync.sh
uv run python3 -m pytest -q -m "not real"
```

## Release

```bash
# Default: test + build + twine check (no upload)
./build.sh

# Build only
./build.sh build

# Release to pypi (upload enabled explicitly)
TWINE_UPLOAD=1 ./build.sh release --repo pypi

# Release to testpypi
TWINE_UPLOAD=1 ./build.sh release --repo testpypi

# Upload existing artifacts only
./build.sh upload --repo pypi

# Help
./build.sh help
```

Recommended upload auth: `~/.pypirc` with API token.

## Project Layout

- `codex_python_sdk/`: SDK source code
- `codex_python_sdk/examples/`: runnable demo code
- `tests/`: unit and real-runtime integration tests
- `uv-sync.sh`: dev environment bootstrap
- `build.sh`: build/release script

## Error Types

- `CodexAgenticError`: base SDK error
- `AppServerConnectionError`: app-server transport/setup failure
- `SessionNotFoundError`: unknown thread/session id
- `NotAuthenticatedError`: auth unavailable or invalid
