from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from feishu_assistant.domain import Conversation, ConversationType
from feishu_assistant.oauth import OAuthCredential, OAuthStore, sign_user_cookie
from feishu_assistant.webapp import AssistantWebApp
from feishu_assistant.workspace import UserWorkspace, WorkspaceStore


class FakeOAuthClient:
    base_url = "https://open.test/open-apis"

    def authorization_url(self, state: str) -> str:
        return f"https://accounts.test/authorize?state={state}"

    def exchange_code(self, code: str) -> OAuthCredential:
        assert code == "valid-code"
        now = int(time.time())
        return OAuthCredential(
            open_id="ou_user",
            name="测试用户",
            access_token="access",
            refresh_token="refresh",
            access_expires_at=now + 7200,
            refresh_expires_at=now + 86400,
        )


class FakeCardSender:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict[str, object], str]] = []

    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str:
        self.cards.append((target_open_id, card, idempotency_key))
        return "om_settings"


def _request(
    app: AssistantWebApp,
    path: str,
    *,
    query: str = "",
    cookie: str = "",
    method: str = "GET",
    form: list[tuple[str, str]] | None = None,
) -> tuple[str, dict[str, str], bytes]:
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    request_body = urlencode(form or []).encode()
    body = b"".join(
        app(
            {
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "HTTP_COOKIE": cookie,
                "REQUEST_METHOD": method,
                "CONTENT_LENGTH": str(len(request_body)),
                "wsgi.input": BytesIO(request_body),
            },
            start_response,
        )
    )
    return captured["status"], captured["headers"], body


def _app(tmp_path: Path) -> tuple[AssistantWebApp, OAuthStore]:
    store = OAuthStore(tmp_path / "oauth.sqlite3", tmp_path / "oauth.key")
    return (
        AssistantWebApp(
            store,
            FakeOAuthClient(),  # type: ignore[arg-type]
            WorkspaceStore(tmp_path / "workspace.json"),
            secure_cookie=False,
        ),
        store,
    )


def test_app_redirects_unconnected_user_with_one_time_state(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    status, headers, body = _request(app, "/app")

    assert status.startswith("302")
    assert headers["Location"].startswith("https://accounts.test/authorize?state=")
    assert body == b""


def test_callback_upserts_user_initializes_workspace_and_sets_session(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, store = _app(tmp_path)
    sender = FakeCardSender()
    app.card_sender = sender
    app.app_url = "https://assistant.test/app"
    state = store.issue_state()

    def ensure_workspace(self: Any, user: Any) -> UserWorkspace:
        return UserWorkspace(
            user.open_id,
            "base",
            "测试用户 的工作记录",
            "https://example/base",
            "tbl_message",
            "tbl_meeting",
        )

    monkeypatch.setattr(
        "feishu_assistant.webapp.WorkspaceManager.ensure_workspace",
        ensure_workspace,
    )

    status, headers, body = _request(
        app,
        "/oauth/callback",
        query=f"state={state}&code=valid-code",
    )

    assert status.startswith("303")
    assert headers["Location"] == "/app/conversations"
    assert body == b""
    assert store.is_initialized("ou_user") is True
    assert len(store.users()) == 1
    assert "assistant_user=" in headers["Set-Cookie"]
    assert len(sender.cards) == 1
    assert sender.cards[0][0] == "ou_user"
    assert sender.cards[0][2].startswith("settings-entry-")
    button = sender.cards[0][1]["body"]["elements"][1]  # type: ignore[index]
    assert button["behaviors"] == [  # type: ignore[index]
        {"type": "open_url", "default_url": "https://assistant.test/app"}
    ]

    cookie = sign_user_cookie("ou_user", store.cookie_secret())
    status, headers, body = _request(
        app, "/app", cookie=f"assistant_user={cookie}"
    )
    assert status.startswith("303")
    assert headers["Location"] == "/app/conversations"
    assert body == b""


def test_conversation_page_discovers_disabled_and_saves_per_user_choices(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, store = _app(tmp_path)
    store.upsert(FakeOAuthClient().exchange_code("valid-code"))
    store.mark_initialized("ou_user")
    discovered = [
        Conversation("oc_group", "技术开发测试", ConversationType.GROUP, False),
        Conversation("oc_private", "测试同事", ConversationType.PRIVATE, False),
    ]

    def discover(open_id: str) -> list[Conversation]:
        return app.conversation_store.sync_discovered(open_id, discovered)

    monkeypatch.setattr(app, "_discover_conversations", discover)
    cookie = sign_user_cookie("ou_user", store.cookie_secret())

    status, _, body = _request(
        app,
        "/app/conversations",
        cookie=f"assistant_user={cookie}",
    )

    assert status.startswith("200")
    assert "技术开发测试".encode() in body
    assert "测试同事".encode() in body
    assert b" checked" not in body

    status, _, body = _request(
        app,
        "/app/conversations",
        cookie=f"assistant_user={cookie}",
        method="POST",
        form=[("enabled_chat_id", "oc_group")],
    )

    assert status.startswith("200")
    assert "设置已保存".encode() in body
    preferences = {
        item.chat_id: item.enabled
        for item in app.conversation_store.list_for_user("ou_user")
    }
    assert preferences == {"oc_group": True, "oc_private": False}


def test_conversation_page_requires_an_initialized_session(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    status, headers, body = _request(app, "/app/conversations")

    assert status.startswith("303")
    assert headers["Location"] == "/app"
    assert body == b""


def test_callback_rejects_replayed_state(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    state = store.issue_state()
    assert store.consume_state(state) is True

    status, _, body = _request(
        app,
        "/oauth/callback",
        query=f"state={state}&code=valid-code",
    )

    assert status.startswith("400")
    assert "state 无效或已过期".encode() in body
