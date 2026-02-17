# 配置指南

[English](../config.md) | [简体中文](./config.md)

`codex-python-sdk` 会从三层合并配置：

1. Server 启动配置（`server_config_overrides`）
2. Thread 默认参数（`default_thread_params` + `thread_params`）
3. Turn 默认参数（`default_turn_params` + `turn_params`）

## 1. Server 配置（`server_config_overrides`）

这些值会被序列化为 `codex app-server -c key=value`。

常见键：
- `model`
- `model_reasoning_effort`
- `model_reasoning_summary`
- `model_verbosity`
- `approval_policy`
- `sandbox_mode`
- `tools`
- `web_search`

示例：

```python
client = create_client(
    server_config_overrides={
        "model": "gpt-5",
        "model_reasoning_effort": "high",
        "web_search": "live",
    }
)
```

## 2. Thread 参数

Thread 级参数影响 thread 生命周期（`thread/start`、`thread/resume`）。

常见字段：
- `approvalPolicy`
- `sandbox`
- `model`
- `cwd`
- `developerInstructions`
- `config`

示例：

```python
client = create_client(
    default_thread_params={
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
    }
)
```

## 3. Turn 参数

Turn 级参数影响每次 `turn/start` 调用。

常见字段：
- `effort`
- `summary`
- `model`
- `outputSchema`
- `sandboxPolicy`
- `collaborationMode`

示例：

```python
result = client.responses_create(
    prompt="Answer briefly",
    turn_params={
        "effort": "low",
        "summary": "none",
    },
)
```

## 4. 合并规则

每次请求按如下顺序：
1. `default_thread_params + thread_params`
2. `default_turn_params + turn_params`
3. 若键冲突，请求级参数覆盖默认值
4. SDK 会在 `responses_*` 内部注入 `threadId` 与 `input`

## 5. 实用模板

### 安全默认运行时

```python
client = create_client(
    default_thread_params={
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
    }
)
```

### 高质量推理

```python
client = create_client(
    server_config_overrides={
        "model": "gpt-5",
        "model_reasoning_effort": "high",
        "model_reasoning_summary": "concise",
    },
    default_turn_params={
        "effort": "high",
        "summary": "concise",
    },
)
```

### 低成本快速响应

```python
client = create_client(
    server_config_overrides={
        "model_reasoning_effort": "low",
        "model_verbosity": "low",
    },
    default_turn_params={
        "effort": "low",
        "summary": "none",
    },
)
```

### 显式 Web Search 策略

```python
client = create_client(
    enable_web_search=True,
    server_config_overrides={
        "tools": {"web_search": True},
        "web_search": "live",  # disabled | cached | live
    },
)
```

如果认证缺失/过期，SDK 会抛出 `NotAuthenticatedError`。
