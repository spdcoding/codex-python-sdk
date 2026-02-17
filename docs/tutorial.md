# Tutorial

[English](./tutorial.md) | [简体中文](./zh/tutorial.md)

This tutorial walks through practical usage patterns for `codex-python-sdk`.

## 1. Prerequisites

- Python `3.9+` (recommended: `3.12`)
- `uv` (recommended for development workflows)
- `codex` CLI
- Local authentication done via `codex login`

Install:

```bash
uv add codex-python-sdk
```

## 2. Minimal Request/Response

```python
from codex_python_sdk import create_client

with create_client() as client:
    result = client.responses_create(prompt="Reply with exactly: READY")
    print(result.session_id)
    print(result.text)
```

When `include_events=True`, the returned `AgentResponse` also contains all collected `ResponseEvent` entries.

## 3. Core Mechanism (from `responses_create` to JSON-RPC)

The core can be summarized in one line: the sync client is a facade, while the
real logic lives in `AsyncCodexAgenticClient`, which talks to `codex app-server`
via `stdio + JSON-RPC`.

Main path for one `responses_create(prompt=...)` call:

1. Factory entrypoint  
   `create_client()` returns `CodexAgenticClient` (sync facade).
2. Sync-to-async bridge  
   Sync methods forward into the async client through an internal event-loop thread.
3. Connection and handshake  
   `connect()` starts the `codex app-server` subprocess and performs
   `initialize/initialized`.
4. Unified RPC primitive  
   All RPC methods use `_request(method, params)`: allocate request id, send JSON,
   and await the matching future.
5. Response API composition  
   `responses_events()` runs `thread/start|resume -> turn/start -> notification stream`;  
   `responses_create()` only aggregates that stream into final `AgentResponse.text`.

Minimal skeleton (simplified):

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
        note = await wait_notification(thread_id)            # streaming notifications from app-server
        event = normalize(note)                              # -> ResponseEvent
        yield event
        if event.type == "turn/completed":
            break
```

Suggested source reading order (core files):
- `codex_python_sdk/factory.py`
- `codex_python_sdk/sync_client.py`
- `codex_python_sdk/async_client.py`
- `codex_python_sdk/types.py`

For a complete architecture walkthrough, see `./core_mechanism.md`.

## 4. Reuse Session Across Turns

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

If `thread/resume` returns `no rollout found` / `rollout not found`, the SDK falls back to the original
`session_id` and continues with `turn/start`.

## 5. Stream Structured Events

```python
from codex_python_sdk import create_client

with create_client() as client:
    for event in client.responses_events(prompt="Summarize this repository"):
        print(event.type, event.phase, event.message_text or event.text_delta)
```

Useful event fields:
- `type`
- `phase`
- `session_id`
- `turn_id`
- `item_id`
- `text_delta` / `message_text`

## 6. Render Exec-Style Logs

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

## 7. Async Usage

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

## 8. Structured JSON Output

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

## 9. Authentication Failures

If auth is unavailable or expired, APIs raise `NotAuthenticatedError`.

Recommended handling:

```python
from codex_python_sdk import NotAuthenticatedError, create_client

try:
    with create_client() as client:
        client.responses_create(prompt="Hello")
except NotAuthenticatedError:
    print("Run `codex login` and retry.")
```

## 10. Useful Next Guides

- `./config.md`: configuration model and templates
- `./core_mechanism.md`: architecture-level core control flow
- `./api.md`: full method list
- `./policy.md`: policy hooks and engines
- `./app_server.md`: app-server mental model and JSON-RPC protocol

## 11. Built-in Smoke/Demo CLI

The package includes `codex-python-sdk-demo` with three modes:

```bash
codex-python-sdk-demo --mode smoke  # quick health check
codex-python-sdk-demo --mode demo   # stable API showcase
codex-python-sdk-demo --mode full   # demo + unstable remote/interrupt/compact paths
```

The demo runner is configured for unattended execution with permissive approvals:
- command approvals return `accept`
- file-change approvals return `accept`
- tool user-input requests return empty answers

Use stricter hooks or a policy engine in real deployments.
`--mode full` also depends on remote endpoints and may fail with transient upstream/server errors.
