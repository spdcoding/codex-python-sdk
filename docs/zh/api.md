# API 参考

[English](../api.md) | [简体中文](./api.md)

本页汇总 `codex_python_sdk` 暴露的公共 API。

## 1. 工厂函数

```python
from codex_python_sdk import create_client, create_async_client
```

- `create_client(**kwargs) -> CodexAgenticClient`
- `create_async_client(**kwargs) -> AsyncCodexAgenticClient`

运行时说明：
- `AsyncCodexAgenticClient` 不支持 `reconnect_attempts` 参数。
- 内部 app-server `stderr` 仅保留最近 500 行缓冲。

## 2. 高频响应 API

同步：
- `responses_create(...) -> AgentResponse`
- `responses_events(...) -> Iterator[ResponseEvent]`
- `responses_stream_text(...) -> Iterator[str]`

异步：
- `await responses_create(...) -> AgentResponse`
- `responses_events(...) -> AsyncIterator[ResponseEvent]`
- `responses_stream_text(...) -> AsyncIterator[str]`

## 3. Thread API

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

## 4. Skills / App API

- `skills_list(cwds=None, force_reload=None)`
- `skills_remote_read()`
- `skills_remote_write(hazelnut_id, is_preload)`
- `skills_config_write(path, enabled)`
- `app_list(limit=None, cursor=None)`

## 5. Turn / Review / Model API

- `turn_interrupt(thread_id, turn_id)`
- `turn_steer(thread_id, turn_id, prompt)`
- `review_start(thread_id, target, delivery=None)`
- `model_list(limit=None, cursor=None)`

## 6. Account API

- `account_rate_limits_read()`
- `account_read(refresh_token=None)`

## 7. Config / MCP / Command API

- `command_exec(command, cwd=None, timeout_ms=None, sandbox_policy=None)`
- `config_read(cwd=None, include_layers=False)`
- `config_value_write(key_path, value, merge_strategy="upsert", file_path=None, expected_version=None)`
- `config_batch_write(edits, file_path=None, expected_version=None)`
- `config_requirements_read()`
- `config_mcp_server_reload()`
- `mcp_server_status_list(limit=None, cursor=None)`
- `mcp_server_oauth_login(name, scopes=None, timeout_secs=None)`
- `fuzzy_file_search(query, roots=None, cancellation_token=None)`

## 8. 渲染 API

- `render_exec_style_events(events, ...)`
- `ExecStyleRenderer`

## 9. 关键类型

`AgentResponse`（主要字段）：
- `text`
- `session_id`
- `request_id`
- `tool_name`
- `raw`
- `events`（可选）

`ResponseEvent`（主要字段）：
- `type`
- `phase`
- `text_delta`
- `message_text`
- `session_id`
- `turn_id`
- `item_id`
- `raw`

## 10. 错误类型

- `CodexAgenticError`
- `AppServerConnectionError`
- `SessionNotFoundError`
- `NotAuthenticatedError`
