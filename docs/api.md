# API Reference

[English](./api.md) | [简体中文](./zh/api.md)

This page summarizes public APIs exposed by `codex_python_sdk`.

## 1. Factory Functions

```python
from codex_python_sdk import create_client, create_async_client
```

- `create_client(**kwargs) -> CodexAgenticClient`
- `create_async_client(**kwargs) -> AsyncCodexAgenticClient`

Runtime notes:
- `AsyncCodexAgenticClient` does not accept `reconnect_attempts`.
- Internal app-server `stderr` buffering keeps only the latest 500 lines.

## 2. High-Frequency Response APIs

Sync:
- `responses_create(...) -> AgentResponse`
- `responses_events(...) -> Iterator[ResponseEvent]`
- `responses_stream_text(...) -> Iterator[str]`

Async:
- `await responses_create(...) -> AgentResponse`
- `responses_events(...) -> AsyncIterator[ResponseEvent]`
- `responses_stream_text(...) -> AsyncIterator[str]`

## 3. Thread APIs

- `thread_start(params=None)`
- `thread_read(thread_id, include_turns=False)`
- `thread_list(limit=50, sort_key="updated_at")`
- `thread_archive(thread_id)`
- `thread_fork(thread_id, params=None)`
- `thread_name_set(thread_id, name)`
- `thread_unarchive(thread_id)`
- `thread_compact_start(thread_id)`
- `thread_rollback(thread_id, num_turns)`
- `thread_loaded_list(limit=None, cursor=None)`

## 4. Skills / App APIs

- `skills_list(cwds=None, force_reload=None)`
- `skills_remote_read()`
- `skills_remote_write(hazelnut_id, is_preload)`
- `skills_config_write(path, enabled)`
- `app_list(limit=None, cursor=None)`

## 5. Turn / Review / Model APIs

- `turn_interrupt(thread_id, turn_id)`
- `turn_steer(thread_id, turn_id, prompt)`
- `review_start(thread_id, target, delivery=None)`
- `model_list(limit=None, cursor=None)`

## 6. Account APIs

- `account_rate_limits_read()`
- `account_read(refresh_token=None)`

## 7. Config / MCP / Command APIs

- `command_exec(command, cwd=None, timeout_ms=None, sandbox_policy=None)`
- `config_read(cwd=None, include_layers=False)`
- `config_value_write(key_path, value, merge_strategy="upsert", file_path=None, expected_version=None)`
- `config_batch_write(edits, file_path=None, expected_version=None)`
- `config_requirements_read()`
- `config_mcp_server_reload()`
- `mcp_server_status_list(limit=None, cursor=None)`
- `mcp_server_oauth_login(name, scopes=None, timeout_secs=None)`
- `fuzzy_file_search(query, roots=None, cancellation_token=None)`

## 8. Rendering APIs

- `render_exec_style_events(events, ...)`
- `ExecStyleRenderer`

## 9. Key Types

`AgentResponse` (main fields):
- `text`
- `session_id`
- `request_id`
- `tool_name`
- `raw`
- `events` (optional)

`ResponseEvent` (main fields):
- `type`
- `phase`
- `text_delta`
- `message_text`
- `session_id`
- `turn_id`
- `item_id`
- `raw`

## 10. Error Types

- `CodexAgenticError`
- `AppServerConnectionError`
- `SessionNotFoundError`
- `NotAuthenticatedError`
