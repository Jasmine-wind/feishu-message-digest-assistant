# 开发文档

## 正式运行范围

当前生产链路只有消息摘要：

```text
员工打开 /app 完成 OAuth
-> 在 /app/conversations 选择群聊或真人私聊
-> 08:00 / 12:00 / 18:00 增量读取
-> qwen3.7-flash 结构化总结
-> 应用身份私聊消息卡片
-> 用户自己的「消息摘要」Base 表
```

会议实现、测试和 schema 仍在代码中，但默认 `features.meeting_summary=false`。关闭时不会创建会议客户端、读取 ASR 配置、轮询智能纪要助手或产生会议 OpenAPI 调用。

## 领域模型

```text
User(open_id, name, enabled)
Conversation(chat_id, name, conversation_type, enabled)
```

`User` 同时是消息数据归属者、OAuth Token 所有者、checkpoint scope、卡片接收者和个人 Base 所有者。不支持读取用户 A 后推送给用户 B。

会话偏好以 `user_open_id + chat_id` 存在 OAuth SQLite 中。新发现会话默认关闭；重新发现只更新名称和类型，不覆盖用户选择。

## 主要模块

```text
OAuthStore / FeishuOAuthClient
├── OAuth state 校验
├── code 换 token
├── SQLite 加密持久化
└── access token 自动刷新

AssistantWebApp
├── /app
├── /app/conversations
└── /oauth/callback

UserIdentityClient
├── 用户身份消息读取
├── 群聊与真人私聊发现
├── 成员姓名解析
└── 消息分页

MessageDigest
├── checkpoint 后增量读取
├── LLM 结构化分类
├── 卡片渲染
├── 确定性 UUID
├── in-flight 恢复
└── 推送成功后提交 checkpoint

WorkspaceManager / BitableArchiver
├── 每用户独立 Base
├── 集中 schema
├── archive_key 幂等
└── 推送成功后业务归档
```

## 消息摘要结构

三个业务区块互斥，由当前 Prompt 分类：

- `关键事件`：已经发生、确认或决定的重要事实。
- `待办事项`：需要后续执行的具体行动。
- `其他值得关注`：风险、背景、规则、过程状态、限制或尚未形成行动的提醒。

LLM 为每个条目返回依据消息的 `source_message_id`。程序只接受本批次真实 message ID，并反查会话名称和发送人，组成：

```text
会话名称 · 发送人
```

展示和 Base 序列化格式：

```text
关键事件：内容｜消息出处
待办事项：事项｜负责人｜截止时间｜消息出处
其他值得关注：内容｜消息出处
```

模型不能直接生成会话名称或发送人。旧 in-flight batch 没有出处信息时仍可恢复，并显示“未明确”。

## 状态与幂等

### 消息 checkpoint

默认文件：

```text
artifacts/message-checkpoint.json
```

每个读取身份和用户有独立 scope。摘要发送前把卡片、摘要结构、时间范围、消息数量和 UUID 写入 `inflight_batches`。发送和归档成功后，以一次原子写推进 checkpoint 并删除 in-flight。

若进程在发送后、本地提交前退出，重启会恢复同一个卡片和 UUID，不扩大时间窗口。

### OAuth 与运行索引

默认数据库：

```text
artifacts/oauth-users.sqlite3
```

保存：

- 加密 OAuth Token
- OAuth state
- 会话偏好
- API 调用记录
- Base schema version
- archive_key 本地索引
- 成员姓名缓存

加密密钥位于 `artifacts/oauth-token.key`。数据库和密钥必须配套备份。

### Base 元数据

默认文件：

```text
artifacts/workspace-state.json
```

保存每个用户的 `app_token` 和表 ID。本地元数据丢失时不会通过云端名称搜索旧 Base，因此部署必须持久化该文件。

## Base schema

当前正式使用「消息摘要」表：

```text
日期
摘要主题
时间范围
消息来源
消息数量
关键事件
待办事项
其他值得关注
archive_key
```

字段定义集中在 `workspace.py`。LLM 不允许动态创建字段。会议关闭时，新用户不创建「会议纪要」表；已有会议表和历史数据不删除。

归档顺序固定为：

```text
发送卡片 -> archive_key 幂等写入 Base -> 提交 checkpoint
```

## 调度与重试

`DigestScheduler` 使用 `Asia/Shanghai` 和配置中的 `08:00`、`12:00`、`18:00`。手工 `digest` 与 scheduler 共用 advisory file lock，避免并发执行。

临时网络/API 错误使用有限指数退避。摘要任务连续三个调度周期耗尽重试后退出，由 systemd 报警并重启；永久配置错误不重试。

状态 JSON 和阶段文件通过同目录临时文件、`fsync` 和 `os.replace` 原子提交。

## Feature flags

```toml
[features]
message_digest = true
meeting_summary = false
```

CLI 在创建任何会议 API 或 ASR 客户端前检查会议 flag。会议关闭时，以下命令明确返回错误：

```text
run
download-recording
meeting-trigger
meeting-trigger-scheduler
```

会议实现保留在 `meeting_summary.py`、`meeting_trigger.py` 和相关测试中。

## 正式进程

只运行：

```text
feishu-assistant oauth-web
feishu-assistant digest-scheduler
```

单次验收或受控补跑：

```text
feishu-assistant digest
```

完整服务器步骤见 `docs/deployment.md`。

## 验证

```bash
uv sync --extra test --frozen
uv run pytest
uv run ruff check src tests
uv run mypy src
uv run python -m compileall -q src tests
uv run python -m build --wheel
```
