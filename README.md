# Feishu Message Digest Assistant

A personal Feishu assistant that reads user-selected conversations on a schedule, summarizes work messages with an LLM, sends a private interactive card, and archives the result in the user's own Feishu Base.

```text
User OAuth
-> selected group and human private conversations
-> incremental reads at 08:00 / 12:00 / 18:00
-> qwen3.7-flash structured summary
-> private Feishu card
-> personal 消息摘要 Base table
```

## Features

- Employee self-service Feishu OAuth with encrypted token persistence and automatic refresh
- Group and human private-chat discovery with opt-in conversation preferences
- Per-user incremental checkpoints and recoverable in-flight batches
- Deterministic Feishu message UUIDs and Base archive keys
- Structured summary sections:
  - `关键事件`: content and message origin
  - `待办事项`: task, assignee, deadline, and message origin
  - `其他值得关注`: content and message origin
- Message origin resolved from the real source message as `conversation name · sender name`
- One personal Base per user with idempotent schema provisioning and record archiving
- Local Feishu OpenAPI usage accounting
- Single-instance scheduling and atomic local state writes

Meeting code and tests remain available for future recovery, but the feature is disabled by default and has no production service.

## Requirements

- Python 3.11 or newer
- A Feishu internal application with web, bot, and Base capabilities
- An OpenAI-compatible LLM API key; the default configuration uses Alibaba Cloud Bailian
- `uv` for reproducible installation, or standard `pip`

## Quick Start

Clone and install:

```bash
git clone git@github.com:Jasmine-wind/feishu-message-digest-assistant.git
cd feishu-message-digest-assistant
uv sync --extra test --frozen
```

Create local configuration:

```bash
cp assistant.toml.example assistant.toml
cp .env.example .env
chmod 600 assistant.toml .env
```

Set the Feishu App ID and public OAuth URLs in `assistant.toml`. Set secrets in `.env`:

```dotenv
FEISHU_APP_SECRET=...
LLM_API_KEY=...
```

The program does not load `.env` automatically. For a local Bash session:

```bash
set -a
. ./.env
set +a
```

For Fish:

```fish
for line in (string match -rv '^\s*(#|$)' < .env)
    set pair (string split -m 1 '=' -- $line)
    set -gx $pair[1] $pair[2]
end
```

Start the two production processes:

```bash
uv run feishu-assistant oauth-web
uv run feishu-assistant digest-scheduler
```

The OAuth application is available at `/app`. Employees authorize the application and select which conversations participate in summaries.

## Configuration

Non-secret configuration lives in `assistant.toml`, created from `assistant.toml.example`.

```toml
[features]
message_digest = true
meeting_summary = false

[message_digest]
times = ["08:00", "12:00", "18:00"]
timezone = "Asia/Shanghai"
read_identity = "user"
initial_lookback_hours = 24
retry_attempts = 3

[providers.llm]
model = "qwen3.7-flash"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
enable_thinking = false
```

Environment variables override corresponding TOML settings. Keep `meeting_summary=false` in the current deployment.

## OAuth Scopes

Current message-digest users require:

```text
offline_access
auth:user.id:read
im:chat:read
im:chat:readonly
im:chat.members:read
im:message:readonly
im:message.group_msg:get_as_user
im:message.p2p_msg:get_as_user
base:app:read
base:app:create
base:table:read
base:table:create
base:table:delete
base:field:read
base:field:create
base:record:read
base:record:create
base:view:read
base:view:write_only
```

The application also needs `im:message:send_as_bot` to send private cards.

New conversations are disabled until the employee enables them in `/app/conversations`. Bot and system private chats do not enter the message digest.

## Commands

```bash
# OAuth and conversation settings web service
uv run feishu-assistant oauth-web

# Scheduled message digest
uv run feishu-assistant digest-scheduler

# Controlled single digest for acceptance or recovery
uv run feishu-assistant digest

# Local OpenAPI usage report
uv run feishu-assistant api-usage --day 2026-08-24
```

Meeting commands remain present for compatibility but return an error before any API client is created while `meeting_summary=false`.

## Persistent State

Runtime state is stored under `artifacts/` and must be mounted on persistent storage:

```text
artifacts/oauth-users.sqlite3
artifacts/oauth-token.key
artifacts/message-checkpoint.json
artifacts/workspace-state.json
```

Never commit `.env`, `assistant.toml`, or `artifacts/`. The OAuth database and encryption key must be backed up together.

## Verification

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
uv run python -m compileall -q src tests
uv run python -m build --wheel
```

## Deployment

See [HANDOFF.md](HANDOFF.md) for the current development checkpoint. See [docs/deployment.md](docs/deployment.md) for the complete systemd and reverse-proxy procedure. Architecture and recovery details are in [docs/development.md](docs/development.md).
