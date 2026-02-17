# `codex app-server`：概念模型与 stdio JSON-RPC 协议

[English](../app_server.md) | [简体中文](./app_server.md)

`codex app-server` 是 Codex CLI 在本地提供的运行时：通过 stdio 暴露 JSON-RPC 接口。
`codex-python-sdk` 会启动该子进程，把 JSON-RPC 消息写入 stdin，并从 stdout 读取事件与响应。

本文聚焦“协议与概念模型”，帮助你理解事件流、ID、以及 `thread` / `turn` / `item` 的关系。

如果你想了解 SDK 内部调用链（`_request`、`responses_events`、sync/async 分层），请看
`./core_mechanism.md`。

## 概念模型：`thread` / `turn` / `item`

- `thread`：会话容器（一个对话 session）。在 SDK 中常以 `session_id` 暴露，在协议 params 中通常是 `threadId`。
- `turn`：thread 内的一次执行回合（一次 prompt + 运行时执行）。通过 `turn/start` 创建，通过 `turn/completed` 结束。
- `item`：turn 内的工作单元/输出单元（assistant 消息增量、工具调用、命令执行、文件变更、推理摘要等）。
  多数情况下以 `item/*` 事件出现，并携带 `itemId`。

重要澄清：`thread` / `turn` / `item` 是运行时概念，不等同于“一行 JSON”。协议的最小单位是“一条 JSON-RPC 消息”（一行）。

## 传输层：每行一个 JSON

app-server 的传输是 stdio 上的 newline-delimited JSON-RPC：

- SDK -> server：子进程 stdin
- server -> SDK：子进程 stdout
- server stderr：日志/诊断（不属于 JSON-RPC 协议流）

在本 SDK 实现中，stdout 使用 `readline()` 按行读取，对每个非空行执行 `json.loads()`。

## JSON-RPC 消息分类（协议层）

在协议层可以把消息分成 4 类：

1. 客户端请求（需要响应）：

   - 有 `id` 与 `method`
   - 例：`{"jsonrpc":"2.0","id":3,"method":"turn/start","params":{...}}`

2. 响应（对请求的回复）：

   - 有 `id`，并包含 `result` 或 `error`
   - 例：`{"jsonrpc":"2.0","id":3,"result":{...}}`

3. 通知（事件流）：

   - 有 `method`，但没有 `id`
   - 例：`{"jsonrpc":"2.0","method":"item/agentMessage/delta","params":{...}}`

4. 服务端请求（server 向 client 发请求，client 必须回复）：

   - 有 `id` 与 `method`，方向是 server -> client
   - 常用于审批 / 工具回调 / 用户输入
   - 例：`{"jsonrpc":"2.0","id":"srvreq_1","method":"item/commandExecution/requestApproval","params":{...}}`

`codex-python-sdk` 会用标准 JSON-RPC response（同一个 `id` + `result`）回复服务端请求。

## 完整 stdio Transcript（带注释）

下面是一段端到端示例。每个 JSON 对象对应一行 stdin/stdout。以 `#` 开头的注释行不属于协议，只是标注：

```text
# IN  (request)  thread: -         turn: -         item: -
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"codex-python-sdk","version":"0.1"},"capabilities":{"experimentalApi":true}}}

# OUT (response) thread: -         turn: -         item: -
{"jsonrpc":"2.0","id":1,"result":{"value":"codex app-server/1.2.3"}}

# IN  (notification) thread: -     turn: -         item: -
{"jsonrpc":"2.0","method":"initialized"}

# IN  (request)  thread: -         turn: -         item: -
{"jsonrpc":"2.0","id":2,"method":"thread/start","params":{"approvalPolicy":"on-request","sandbox":"workspace-write"}}

# OUT (response) thread: thread_123 turn: -         item: -
{"jsonrpc":"2.0","id":2,"result":{"thread":{"id":"thread_123"}}}

# IN  (request)  thread: thread_123 turn: -         item: -
{"jsonrpc":"2.0","id":3,"method":"turn/start","params":{"threadId":"thread_123","input":[{"type":"text","text":"List files in the repo root","text_elements":[]}]}}

# OUT (response) thread: thread_123 turn: turn_abc  item: -
{"jsonrpc":"2.0","id":3,"result":{"turn":{"id":"turn_abc"}}}

# OUT (notification) thread: thread_123 turn: turn_abc item: item_plan_1
{"jsonrpc":"2.0","method":"item/plan/delta","params":{"threadId":"thread_123","turnId":"turn_abc","itemId":"item_plan_1","delta":"Plan: run `ls` then summarize."}}

# OUT (server request; needs reply) thread: thread_123 turn: turn_abc item: item_cmd_1
{"jsonrpc":"2.0","id":"srvreq_1","method":"item/commandExecution/requestApproval","params":{"threadId":"thread_123","turnId":"turn_abc","itemId":"item_cmd_1","command":"ls"}}

# IN  (response to server request) thread: thread_123 turn: turn_abc item: item_cmd_1
{"jsonrpc":"2.0","id":"srvreq_1","result":{"decision":"accept"}}

# OUT (notification) thread: thread_123 turn: turn_abc item: item_cmd_1
{"jsonrpc":"2.0","method":"item/commandExecution/outputDelta","params":{"threadId":"thread_123","turnId":"turn_abc","itemId":"item_cmd_1","delta":"README.md\\ncodex_python_sdk\\ntests\\n"}}

# OUT (notification) thread: thread_123 turn: turn_abc item: item_cmd_1
{"jsonrpc":"2.0","method":"item/completed","params":{"threadId":"thread_123","turnId":"turn_abc","itemId":"item_cmd_1","item":{"type":"commandExecution","status":"completed"}}}

# OUT (notification) thread: thread_123 turn: turn_abc item: item_msg_1
{"jsonrpc":"2.0","method":"item/agentMessage/delta","params":{"threadId":"thread_123","turnId":"turn_abc","itemId":"item_msg_1","delta":"Repo root contains README.md, codex_python_sdk/, tests/."}}

# OUT (notification) thread: thread_123 turn: turn_abc item: item_msg_1
{"jsonrpc":"2.0","method":"item/completed","params":{"threadId":"thread_123","turnId":"turn_abc","itemId":"item_msg_1","item":{"type":"agentMessage","text":"Repo root contains README.md, codex_python_sdk/, tests/."}}}

# OUT (notification) thread: thread_123 turn: turn_abc item: -
{"jsonrpc":"2.0","method":"turn/completed","params":{"threadId":"thread_123","turn":{"id":"turn_abc","status":"completed"}}}
```

## ID 出现位置（以及为什么缺失是正常的）

理想情况下，通知会带 `threadId`、`turnId`，以及可选的 `itemId`。但部分事件可能会缺失其中某个 ID。
SDK 会用“当前 thread_id”以及 `turn/start` 返回的 `turn_id` 来做匹配与上下文补全。

## 服务端请求：审批与工具回调

turn 执行过程中，app-server 可能向 client 发出请求，让 client 决策/输入。它们是 server -> client 的 JSON-RPC request，必须回复：

- `item/commandExecution/requestApproval`：命令执行审批。
- `item/fileChange/requestApproval`：文件变更审批。
- `item/tool/requestUserInput`：工具需要用户输入时的答案。
- `item/tool/call`：当配置了 client-side tool handler 时的工具调用回调。

SDK 通过 hooks（或可选的 `PolicyEngine`）处理这些请求，并返回对应的 `result`。

## API 分类概览（以本 SDK 实际调用为准）

本节按类别列出 `codex-python-sdk` 会调用的主要 JSON-RPC methods。它不是 app-server 全部接口的完整清单。

### Thread / session 生命周期

- `thread/start`：创建 thread（返回 `thread.id`）。
- `thread/resume`：通过 `threadId` 恢复 thread（可能因 thread 不可用而失败）。
- `thread/read`：读取 thread 状态（可选择包含 turns）。
- `thread/list`：列出 threads。
- `thread/archive` / `thread/unarchive`：归档管理。
- `thread/name/set`：设置 thread 名称。
- `thread/fork`：fork thread。
- `thread/compact/start`：compact/summarize thread。
- `thread/rollback`：回滚 turns。
- `thread/loaded/list`：列出运行时已加载的 threads。

最常用：`thread/start`、`thread/resume`、`thread/read`。

### Turn 执行

- `turn/start`：在 `threadId` 上启动 turn，携带 `input`（返回 `turn.id`）。
- `turn/interrupt`：中断正在运行的 turn。

最关键：`turn/start` 以及最终的 `turn/completed` 通知。

### stdout 上的事件流（通知）

常见通知族：

- `item/agentMessage/*`：assistant 消息流式输出与完成。
- `item/plan/*`：计划增量（`item/plan/delta`）与更新（`turn/plan/updated`）。
- `item/commandExecution/*`：命令输出增量与审批。
- `item/fileChange/*`：文件变更输出增量与审批。
- `thread/*`、`account/*`：状态、用量等更新。
- `turn/*`：完成、diff 更新、plan 更新等。

做 UX 最关键：`item/agentMessage/delta`、`item/completed`、`turn/completed`。

### 管理与发现

- `model/list`：列出可用模型。
- `account/read`、`account/rateLimits/read`：账户信息与限额。

### 配置

- `config/read`：读取配置。
- `config/value/write` / `config/batchWrite`：写配置。
- `configRequirements/read`：读取配置要求。
- `config/mcpServer/reload`：重载 MCP server 配置。

### Skills / apps / 其他

- `skills/list`、`skills/remote/read`、`skills/config/write`
- `app/list`
- `fuzzyFileSearch`
- `review/start`
- `mcpServerStatus/list`、`mcpServer/oauth/login`

## 实用建议

- 只需要最终文本：用 `responses_create(...)`。
- 需要构建 UI/CLI、展示进度：消费 `responses_events(...)`，把它当作“运行时在做什么”的权威事件流。
- 需要严格控制命令/文件写入：配置 hooks 或 `PolicyEngine`。
