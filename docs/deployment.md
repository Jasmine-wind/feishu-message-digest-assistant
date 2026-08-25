# Deployment

The production runtime consists of two processes only:

```text
feishu-assistant oauth-web
feishu-assistant digest-scheduler
```

Meeting processing is disabled by default and must not have a service unit.

## 1. Host setup

Install Python 3.11 or newer and `uv`, then create a dedicated account:

```bash
sudo useradd --system --create-home --home-dir /opt/feishu-message-digest-assistant \
  --shell /usr/sbin/nologin feishu-assistant
sudo mkdir -p /opt/feishu-message-digest-assistant
sudo chown feishu-assistant:feishu-assistant /opt/feishu-message-digest-assistant
```

Clone and install:

```bash
sudo -u feishu-assistant git clone \
  git@github.com:Jasmine-wind/feishu-message-digest-assistant.git \
  /opt/feishu-message-digest-assistant
cd /opt/feishu-message-digest-assistant
sudo -u feishu-assistant uv sync --frozen
```

## 2. Configuration

Create the non-secret configuration:

```bash
sudo -u feishu-assistant cp assistant.toml.example assistant.toml
sudo -u feishu-assistant chmod 600 assistant.toml
sudo -u feishu-assistant mkdir -p artifacts
```

Edit `assistant.toml` and set at minimum:

```toml
[features]
message_digest = true
meeting_summary = false

[feishu]
app_id = "cli_your_app_id"

[oauth]
public_url = "https://assistant.example.com"
redirect_uri = "https://assistant.example.com/oauth/callback"
```

Create the systemd environment file:

```bash
sudo install -d -m 700 /etc/feishu-message-digest-assistant
sudo install -m 640 -o root -g feishu-assistant \
  .env.example /etc/feishu-message-digest-assistant/env
sudo editor /etc/feishu-message-digest-assistant/env
```

Required secrets:

```dotenv
ASSISTANT_CONFIG_FILE=assistant.toml
FEISHU_APP_SECRET=...
LLM_API_KEY=...
```

`ASR_API_KEY` is not required while meeting processing is disabled.

## 3. Feishu application

Configure the Feishu application with:

- Web application home: `https://assistant.example.com/app`
- OAuth redirect: `https://assistant.example.com/oauth/callback`
- Bot permission to send private cards
- The user OAuth scopes listed in the project README
- An application availability range that includes target employees

Publish a new application version after changing scopes or URLs.

## 4. Reverse proxy

Terminate HTTPS in Nginx, Caddy, or the platform ingress and proxy the public host to:

```text
127.0.0.1:8765
```

The following paths must remain reachable:

```text
/app
/app/conversations
/oauth/callback
```

## 5. systemd

Install and start the provided units:

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-oauth-web.service
sudo systemctl enable --now feishu-digest-scheduler.service
```

Check status and logs:

```bash
systemctl status feishu-oauth-web.service feishu-digest-scheduler.service
journalctl -u feishu-oauth-web.service -u feishu-digest-scheduler.service -f
```

Do not install or start `meeting-trigger-scheduler`.

## 6. Persistent data

Persist and back up the entire `artifacts/` directory. The critical files are:

```text
artifacts/oauth-users.sqlite3
artifacts/oauth-token.key
artifacts/message-checkpoint.json
artifacts/workspace-state.json
```

The OAuth database and encryption key must be backed up together. Keep both at mode `0600` and never commit them to Git.

## 7. Acceptance

1. Open `/app` from the Feishu workbench and complete OAuth.
2. Select one or more group or human private conversations.
3. Run one controlled digest:

   ```bash
   sudo -u feishu-assistant bash -c '
     set -a
     . /etc/feishu-message-digest-assistant/env
     set +a
     cd /opt/feishu-message-digest-assistant
     .venv/bin/feishu-assistant digest
   '
   ```

4. Confirm the private card and the user's `消息摘要` Base record.
5. Confirm meeting traffic remains zero:

   ```bash
   feishu-assistant api-usage --day YYYY-MM-DD --caller meeting_trigger
   ```

6. Confirm scheduler times are `08:00`, `12:00`, and `18:00` in `Asia/Shanghai`.

## 8. Upgrade

```bash
cd /opt/feishu-message-digest-assistant
sudo -u feishu-assistant git pull --ff-only
sudo -u feishu-assistant uv sync --frozen
sudo systemctl restart feishu-oauth-web.service feishu-digest-scheduler.service
```
