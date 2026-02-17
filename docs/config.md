# Configuration Guide

[English](./config.md) | [简体中文](./zh/config.md)

`codex-python-sdk` merges configuration from three layers:

1. Server launch config (`server_config_overrides`)
2. Thread defaults (`default_thread_params` + `thread_params`)
3. Turn defaults (`default_turn_params` + `turn_params`)

## 1. Server Config (`server_config_overrides`)

Values are serialized into `codex app-server -c key=value`.

Common keys:
- `model`
- `model_reasoning_effort`
- `model_reasoning_summary`
- `model_verbosity`
- `approval_policy`
- `sandbox_mode`
- `tools`
- `web_search`

Example:

```python
client = create_client(
    server_config_overrides={
        "model": "gpt-5",
        "model_reasoning_effort": "high",
        "web_search": "live",
    }
)
```

## 2. Thread Params

Thread-level params affect thread lifecycle (`thread/start`, `thread/resume`).

Common fields:
- `approvalPolicy`
- `sandbox`
- `model`
- `cwd`
- `developerInstructions`
- `config`

Example:

```python
client = create_client(
    default_thread_params={
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
    }
)
```

## 3. Turn Params

Turn-level params affect each `turn/start` call.

Common fields:
- `effort`
- `summary`
- `model`
- `outputSchema`
- `sandboxPolicy`
- `collaborationMode`

Example:

```python
result = client.responses_create(
    prompt="Answer briefly",
    turn_params={
        "effort": "low",
        "summary": "none",
    },
)
```

## 4. Merge Rules

For each request:
1. `default_thread_params + thread_params`
2. `default_turn_params + turn_params`
3. Request-level values override defaults on key conflicts
4. SDK injects `threadId` and `input` internally for `responses_*`

## 5. Practical Templates

### Safe default runtime

```python
client = create_client(
    default_thread_params={
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
    }
)
```

### High-quality reasoning

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

### Low-cost fast responses

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

### Explicit web search policy

```python
client = create_client(
    enable_web_search=True,
    server_config_overrides={
        "tools": {"web_search": True},
        "web_search": "live",  # disabled | cached | live
    },
)
```

If auth is missing/expired, SDK raises `NotAuthenticatedError`.
