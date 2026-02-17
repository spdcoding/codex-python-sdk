# `codex app-server`: Mental Model + JSON-RPC Protocol (stdio)

[English](./app_server.md) | [简体中文](./zh/app_server.md)

`codex app-server` is a local JSON-RPC runtime exposed over stdio. `codex-python-sdk` starts the
process, sends JSON-RPC messages to its stdin, and reads JSON-RPC messages from its stdout.

This document focuses on the protocol and the runtime mental model so you can reason about events,
IDs, and where `thread` / `turn` / `item` fit in.

If you want SDK internal control flow (`_request`, `responses_events`, sync/async layering), see
`./core_mechanism.md`.

## Mental Model: `thread` / `turn` / `item`

- `thread`: A conversation container (a session). In SDK APIs this is commonly surfaced as
  `session_id`, and in protocol params as `threadId`.
- `turn`: One execution step inside a thread (one prompt + the runtime doing work). `turn/start`
  creates a new turn; completion is signaled via `turn/completed`.
- `item`: A unit of work or output produced within a turn (assistant message deltas, tool calls,
  command execution, file change, reasoning summaries, etc). Items are typically described through
  `item/*` events and carry an `itemId`.

Important: `thread` / `turn` / `item` are runtime concepts. They are not the same thing as "one JSON
line". The smallest protocol unit is one JSON-RPC message (one line on stdout/stderr).

## Transport: One JSON per line

The app-server transport is JSON-RPC over stdio with newline-delimited JSON.

- SDK -> server: subprocess stdin
- server -> SDK: subprocess stdout
- server stderr: logs/diagnostics (not part of JSON-RPC)

In this SDK implementation, stdout is read with `readline()`, then `json.loads()` is applied to
each non-empty line.

## Message Types (JSON-RPC layer)

At the protocol layer there are four relevant shapes:

1. Client request (needs a response):

   - Has `id` and `method`
   - Example: `{"jsonrpc":"2.0","id":3,"method":"turn/start","params":{...}}`

2. Response (reply to a request):

   - Has `id`, and either `result` or `error`
   - Example: `{"jsonrpc":"2.0","id":3,"result":{...}}`

3. Notification (event stream):

   - Has `method`, but no `id`
   - Example: `{"jsonrpc":"2.0","method":"item/agentMessage/delta","params":{...}}`

4. Server request (server asks client to reply):

   - Has `id` and `method`, but the direction is server -> client
   - Used for approvals / tool callbacks / user input
   - Example: `{"jsonrpc":"2.0","id":"srvreq_1","method":"item/commandExecution/requestApproval","params":{...}}`

`codex-python-sdk` replies to server requests with a normal JSON-RPC response carrying the same
`id` and a `result` object.

## Annotated Full stdio Transcript

Below is an end-to-end example transcript. Each JSON is one stdout/stdin line. The comment lines
are not transmitted; they are annotations to help you map concepts:

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

## Where IDs Appear (and why missing IDs are OK)

In the ideal case, each notification includes `threadId`, `turnId`, and sometimes `itemId`.
However, in practice some events may omit one or more IDs. The SDK uses the current `thread_id`
and the `turn_id` returned by `turn/start` to filter and attach context as needed.

## Server Requests: Approvals and Tool Callbacks

During a turn, app-server may ask the client to decide/answer something. These are JSON-RPC
requests from server -> client and require a response:

- `item/commandExecution/requestApproval`: approve or decline executing a shell command.
- `item/fileChange/requestApproval`: approve or decline applying file changes.
- `item/tool/requestUserInput`: provide answers for tool input prompts.
- `item/tool/call`: handle a tool call when a client-side tool handler is configured.

The SDK exposes these via hooks (or an optional policy engine) and replies with a `result` object.

## API Surface Overview (as used by this SDK)

This section groups the most important JSON-RPC methods that `codex-python-sdk` calls.
It is not meant to be a complete list of every app-server method.

### Thread/session lifecycle

- `thread/start`: create a new thread (returns `thread.id`).
- `thread/resume`: resume an existing thread by `threadId` (may fail if the thread is not available).
- `thread/read`: read thread state (optionally include turns).
- `thread/list`: list recent threads.
- `thread/archive` / `thread/unarchive`: manage archival state.
- `thread/name/set`: assign a human-friendly name.
- `thread/fork`: fork a thread.
- `thread/compact/start`: compact/summarize a thread.
- `thread/rollback`: rollback turns.
- `thread/loaded/list`: list loaded threads in the runtime.

Most important for day-to-day use: `thread/start`, `thread/resume`, `thread/read`.

### Turn execution

- `turn/start`: start one turn with `threadId` and `input` (returns `turn.id`).
- `turn/interrupt`: request interruption of a running turn.

Most important: `turn/start` and the `turn/completed` notification.

### Streaming events you will see on stdout

Common notification families:

- `item/agentMessage/*`: assistant message streaming and completion.
- `item/plan/*`: plan deltas (`item/plan/delta`) and updates (`turn/plan/updated`).
- `item/commandExecution/*`: command output deltas and approvals.
- `item/fileChange/*`: file change output deltas and approvals.
- `thread/*` and `account/*`: state and usage updates.
- `turn/*`: completion, diff updates, plan updates.

Most important for building UX: `item/agentMessage/delta`, `item/completed`, `turn/completed`.

### Admin / discovery

- `model/list`: list available models.
- `account/read` and `account/rateLimits/read`: account metadata and rate limits.

### Config

- `config/read`: read config.
- `config/value/write` / `config/batchWrite`: write config values.
- `configRequirements/read`: discover config requirements.
- `config/mcpServer/reload`: reload MCP server config.

### Skills / apps / misc

- `skills/list`, `skills/remote/read`, `skills/config/write`
- `app/list`
- `fuzzyFileSearch`
- `review/start`
- `mcpServerStatus/list`, `mcpServer/oauth/login`

## Practical Guidance

- If you only need the assistant final output, use `responses_create(...)`.
- If you are building a UI/CLI and need progress, consume `responses_events(...)` and treat it as
  the canonical stream of "what the runtime is doing".
- If your code must approve/deny commands and file changes, configure hooks or a `PolicyEngine`.
