# HANDOFF

## 1. Project

这是一个个人飞书助手：读取用户主动选择的群聊和真人私聊，按时间窗口生成工作消息摘要，私聊发送交互卡片，并归档到该用户自己的飞书多维表格。

当前技术栈是 Python 3.11+、`uv`、`httpx`、OpenAI-compatible LLM client、SQLite 和飞书 OpenAPI。当前阶段是**消息摘要 MVP 的部署交接**；会议实现保留在代码中，但当前默认关闭且没有生产服务。

## 2. Current State

仓库和测试能够证明的当前能力：

- `/app`、`/oauth/callback` 和 `/app/conversations` 提供用户 OAuth、工作区初始化和会话选择。
- 新发现会话默认关闭；只读取用户选择的群聊和真人私聊，机器人/系统私聊不进入摘要。
- `digest` 和 `digest-scheduler` 按用户增量读取消息，使用 LLM 结构化总结，发送私卡并写入用户自己的「消息摘要」Base。
- checkpoint、in-flight batch、确定性消息 UUID 和 `archive_key` 支持失败恢复与幂等归档。
- `api-usage` 读取本地 SQLite 中的 OpenAPI 调用记录。
- 默认配置为 `features.message_digest=true`、`features.meeting_summary=false`。会议 CLI 在关闭时会在创建 API/ASR 客户端前返回错误。

本次交接没有重新执行真实飞书 OAuth、飞书 API 或 LLM 的线上端到端验证；线上状态应按第 9 节处理。

## 3. How to Run

环境要求：Python 3.11+、`uv`、一个具备 OAuth/Bot/Base 权限的飞书自建应用，以及一个 OpenAI-compatible LLM API。

开发安装：

```bash
git clone git@github.com:Jasmine-wind/feishu-message-digest-assistant.git
cd feishu-message-digest-assistant
uv sync --extra test --frozen
cp assistant.toml.example assistant.toml
cp .env.example .env
chmod 600 assistant.toml .env
$EDITOR assistant.toml   # 至少设置 [feishu].app_id、[oauth] URL
$EDITOR .env              # 设置 FEISHU_APP_SECRET、LLM_API_KEY
```

程序不会自动读取 `.env`。在每个运行终端先加载配置：

```bash
set -a
. ./.env
set +a
```

终端 A 启动 OAuth Web 服务：

```bash
uv run feishu-assistant oauth-web
```

在配置的 public URL 下打开 `/app`，完成 OAuth 并在 `/app/conversations` 勾选会话。终端 B 使用同样的环境变量启动调度器：

```bash
uv run feishu-assistant digest-scheduler
```

受控单次运行：

```bash
uv run feishu-assistant digest
```

首次启动会在 `artifacts/` 下自动创建 SQLite 表和状态文件；没有独立 migration 命令。OAuth 回调会为用户初始化飞书 Base。生产 systemd、反向代理和验收步骤见 [`docs/deployment.md`](docs/deployment.md)。

## 4. Verification

交接前实际执行：

- `uv sync --extra test --frozen`：成功。
- `uv run pytest`：`99 passed`。
- `uv run ruff check src tests`：通过。
- `uv run mypy src`：通过。
- `uv run python -m compileall -q src tests`：通过。
- `uv run python -m build --wheel`：成功生成 wheel。

自动化 E2E 命令：`Unknown`；线上飞书/LLM 验证未在本次交接中执行。CI 对应配置在 `.github/workflows/ci.yml`。

## 5. Architecture & Main Chain

- 主入口是 `src/feishu_assistant/cli.py`，`__main__.py` 和 `pyproject.toml` 暴露 `feishu-assistant` 命令。
- `oauth-web` 使用 `webapp.AssistantWebApp` 完成 OAuth、会话发现和选择保存。
- `digest-scheduler`/`digest` 使用用户 OAuth 读取已启用会话；`LLMClient` 生成摘要；`FeishuClient` 以应用身份发送卡片；`BitableArchiver` 归档。
- 消息摘要的 checkpoint 在 `artifacts/message-checkpoint.json`；OAuth、会话偏好、缓存、幂等索引和 API 使用记录共用 `artifacts/oauth-users.sqlite3`。
- `artifacts/oauth-token.key` 加密 OAuth token；`artifacts/workspace-state.json` 保存用户 Base/table 元数据。数据库和 key 必须一起备份。
- Base 字段定义的 SoT 是 `src/feishu_assistant/workspace.py`；当前 schema version 为代码中的 `WORKSPACE_SCHEMA_VERSION=1`。没有独立 migration 目录，启动代码负责建表及已有数据库兼容处理。
- 主业务顺序固定为：发送卡片 → 使用 `archive_key` 幂等写入 Base → 提交 checkpoint。

## 6. Repository Map

```text
src/feishu_assistant/  核心 CLI、配置、OAuth、飞书客户端、摘要、归档和调度
 tests/                 单元测试和集成边界测试
 docs/                  当前部署与开发/恢复说明
 deploy/systemd/        OAuth Web 和摘要调度器 service unit
 .github/workflows/     CI 验证流程
 pyproject.toml         项目元数据、依赖和命令入口
 uv.lock                锁定依赖
 assistant.toml.example 非敏感 TOML 配置模板
 .env.example           环境变量模板
 artifacts/             本地持久化运行状态；被 Git 忽略
```

## 7. Configuration

- 配置入口默认是根目录 `assistant.toml`，也可用 `ASSISTANT_CONFIG_FILE` 指定；环境变量优先于 TOML。
- 非敏感配置模板是 `assistant.toml.example`。正常消息摘要至少需要 `[feishu].app_id`、可访问的 OAuth `public_url`/`redirect_uri`，以及可写的 `artifacts/`。
- 密钥来自环境变量或部署平台 secret store：`FEISHU_APP_SECRET`、`LLM_API_KEY`。`.env` 不会自动加载，systemd 使用 `docs/deployment.md` 中的 `EnvironmentFile`。
- 默认 LLM 是 `qwen3.7-flash` / DashScope；模型、base URL 和 thinking 选项可在 `[providers.llm]` 或对应环境变量中覆盖。
- 会议重新启用时才需要 `[providers.asr]`、`ASR_API_KEY` 和会议 OAuth scopes；不要只改 feature flag 而跳过线上验证。
- 不得提交 `.env`、`assistant.toml`、`artifacts/`、token/key、数据库、媒体和构建/缓存文件。

## 8. Current Constraints / Invariants

- 用户身份、消息归属、卡片接收者、Base 所有者和 checkpoint scope 必须是同一个 `User.open_id`。详见 [`docs/development.md`](docs/development.md)。
- 新会话默认关闭；设置页是 OAuth 用户会话发现和启用的入口。调度摘要只消费已保存的 enabled 会话。
- `features.meeting_summary=false` 时只部署 `oauth-web` 和 `digest-scheduler`，不要安装 `meeting-trigger-scheduler`。
- 不要让 LLM 输出驱动 Base schema；字段定义集中在 `workspace.py`。修改 schema 时同步处理 schema version 和已有用户数据。
- 发送、归档、checkpoint 的顺序以及 in-flight 恢复语义不可随意改变；SQLite/key、checkpoint 和 workspace state 都需要持久化。

## 9. Known Issues / Risks

- 真实飞书应用的权限、OAuth redirect URL、反向代理 HTTPS、LLM 额度和当前线上数据状态在本次交接中为 `Unknown`，需要部署时做一次受控 digest 验收。
- 当前没有独立前端、worker 或 migration 工具；Web UI 是 `webapp.py` 中的服务端 HTML，两个 systemd 进程构成主运行链路。
- Base 当前实现以写入和幂等归档为主，CLI 没有记录删除流程。
- 兼容路径使用 `FEISHU_USER_OAUTH_PROFILES`/旧 token 配置时会调用外部 `lark-cli`；正常 Web OAuth 用户主链路使用本地加密 token，不应假设两条路径环境相同。
- 本地 `artifacts/` 可能包含现有 OAuth 凭据和业务状态，未纳入 Git；迁移机器前必须确认备份与权限，不能用空目录覆盖生产状态。

## 10. Recommended Next Work

1. 按 `docs/deployment.md` 配置真实飞书应用、OAuth URL、secret store 和两个 systemd 服务。
2. 完成一次真实 OAuth → 选择会话 → `digest` → 私卡/Base/checkpoint 的受控验收，并观察 `api-usage` 与 systemd 日志。
3. 固化 `artifacts/` 的备份/恢复演练；处理验收中发现的权限、配额或运行问题。
4. 会议功能只有在重新确认产品范围、权限和部署策略后才继续，不要在当前默认关闭状态下擅自上线。

仓库没有当前有效的 `PLAN`/`CONTEXT` 文件；以上顺序仅基于当前代码和正式开发/部署文档，不代表新增 roadmap。

## 11. Git Handoff Point

- branch：`main`
- cleanup 前可恢复且已验证、已 push 的 checkpoint：`1231bad3128bd8e6c1c967faea7cf3da82bd3ce6`
- 最终清理 commit SHA：以交接提交后的 `git rev-parse HEAD` 为准，并在交接结果中报告。
- working tree：最终提交后应为 clean。
- push：cleanup 前 checkpoint 已 push；最终清理 commit 需确认已 push。
