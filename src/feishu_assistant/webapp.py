from __future__ import annotations

import html
import logging
from collections.abc import Callable, Iterable
from http import HTTPStatus
from http.cookies import SimpleCookie
from socketserver import ThreadingMixIn
from typing import Protocol
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server

from .conversation_preferences import ConversationPreferenceStore
from .domain import Conversation, ConversationType, User
from .errors import AssistantError, ConfigurationError
from .oauth import (
    FeishuOAuthClient,
    OAuthStore,
    OAuthTokenProvider,
    StoredOAuthRunner,
    sign_user_cookie,
    verify_user_cookie,
)
from .runtime_store import RuntimeStore
from .settings_card import build_settings_entry_card, settings_card_idempotency_key
from .user_identity_client import UserIdentityClient
from .workspace import BitableClient, WorkspaceManager, WorkspaceStore

StartResponse = Callable[[str, list[tuple[str, str]]], object]


class _CardSender(Protocol):
    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str: ...


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class AssistantWebApp:
    def __init__(
        self,
        oauth_store: OAuthStore,
        oauth_client: FeishuOAuthClient,
        workspace_store: WorkspaceStore,
        *,
        runtime_store: RuntimeStore | None = None,
        secure_cookie: bool = True,
        card_sender: _CardSender | None = None,
        app_url: str = "",
        include_meeting: bool = True,
    ) -> None:
        self.oauth_store = oauth_store
        self.oauth_client = oauth_client
        self.workspace_store = workspace_store
        self.conversation_store = ConversationPreferenceStore(
            oauth_store.database_path
        )
        self.runtime_store = runtime_store or RuntimeStore(oauth_store.database_path)
        self.secure_cookie = secure_cookie
        self.card_sender = card_sender
        self.app_url = app_url
        self.include_meeting = include_meeting

    def __call__(
        self, environ: dict[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        path = str(environ.get("PATH_INFO", ""))
        if path == "/app":
            return self._app(environ, start_response)
        if path == "/app/conversations":
            return self._conversations(environ, start_response)
        if path == "/oauth/callback":
            return self._callback(environ, start_response)
        return self._html(start_response, HTTPStatus.NOT_FOUND, "页面不存在")

    def _app(
        self, environ: dict[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        open_id = self._session_open_id(environ)
        if open_id and self.oauth_store.is_initialized(open_id):
            return self._redirect(start_response, "/app/conversations")
        state = self.oauth_store.issue_state()
        location = self.oauth_client.authorization_url(state)
        start_response(
            f"{HTTPStatus.FOUND.value} {HTTPStatus.FOUND.phrase}",
            [("Location", location), ("Cache-Control", "no-store")],
        )
        return [b""]

    def _callback(
        self, environ: dict[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        query = parse_qs(str(environ.get("QUERY_STRING", "")), keep_blank_values=True)
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        if not state or not self.oauth_store.consume_state(state):
            return self._html(
                start_response,
                HTTPStatus.BAD_REQUEST,
                "授权失败",
                "state 无效或已过期，请重新打开应用。",
            )
        if not code:
            return self._html(
                start_response,
                HTTPStatus.BAD_REQUEST,
                "授权失败",
                "飞书未返回授权码，请重新同意授权。",
            )
        try:
            credential = self.oauth_client.exchange_code(code)
            self.oauth_store.upsert(credential)
            provider = OAuthTokenProvider(
                self.oauth_store, self.oauth_client, credential.open_id
            )
            runner = StoredOAuthRunner(
                provider,
                base_url=self.oauth_client.base_url,
                recorder=getattr(self.oauth_client, "recorder", None),
                user_open_id=credential.open_id,
            )
            client = BitableClient(credential.open_id, runner=runner)
            WorkspaceManager(
                client,
                self.workspace_store,
                self.runtime_store,
                include_meeting=self.include_meeting,
            ).ensure_workspace(User(credential.open_id, credential.name))
            self.oauth_store.mark_initialized(credential.open_id)
        except AssistantError as exc:
            logging.getLogger(__name__).exception("OAuth onboarding failed")
            return self._html(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                "授权已完成，但初始化失败",
                str(exc),
            )
        self._send_settings_entry_card(credential.open_id)
        cookie = SimpleCookie()
        cookie["assistant_user"] = sign_user_cookie(
            credential.open_id, self.oauth_store.cookie_secret()
        )
        cookie["assistant_user"]["path"] = "/"
        cookie["assistant_user"]["httponly"] = True
        cookie["assistant_user"]["samesite"] = "Lax"
        if self.secure_cookie:
            cookie["assistant_user"]["secure"] = True
        header = cookie.output(header="").strip()
        return self._redirect(
            start_response,
            "/app/conversations",
            extra_headers=[("Set-Cookie", header)],
        )

    def _send_settings_entry_card(self, open_id: str) -> None:
        if self.card_sender is None or not self.app_url:
            return
        try:
            self.card_sender.send_private_card(
                open_id,
                build_settings_entry_card(self.app_url),
                idempotency_key=settings_card_idempotency_key(open_id),
            )
        except (AssistantError, ValueError):
            logging.getLogger(__name__).exception(
                "unable to send settings entry card user=%s", open_id
            )

    def _conversations(
        self, environ: dict[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        open_id = self._session_open_id(environ)
        if not open_id or not self.oauth_store.is_initialized(open_id):
            return self._redirect(start_response, "/app")
        try:
            conversations = self._discover_conversations(open_id)
            saved = False
            if str(environ.get("REQUEST_METHOD", "GET")).upper() == "POST":
                form = self._read_form(environ)
                enabled = set(form.get("enabled_chat_id", []))
                visible = {conversation.chat_id for conversation in conversations}
                self.conversation_store.save_enabled(open_id, visible, enabled)
                conversations = self.conversation_store.sync_discovered(
                    open_id, conversations
                )
                saved = True
        except ValueError as exc:
            return self._html(
                start_response,
                HTTPStatus.BAD_REQUEST,
                "保存失败",
                str(exc),
            )
        except AssistantError as exc:
            logging.getLogger(__name__).exception(
                "conversation preference page failed user=%s", open_id
            )
            return self._html(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                "消息来源加载失败",
                str(exc),
            )
        return self._conversation_page(start_response, conversations, saved=saved)

    def _discover_conversations(self, open_id: str) -> list[Conversation]:
        credential = self.oauth_store.get(open_id)
        if credential is None:
            raise ConfigurationError("当前 OAuth 用户不存在，请重新授权。")
        provider = OAuthTokenProvider(self.oauth_store, self.oauth_client, open_id)
        runner = StoredOAuthRunner(
            provider,
            base_url=self.oauth_client.base_url,
            recorder=getattr(self.oauth_client, "recorder", None),
            user_open_id=open_id,
        )
        source = UserIdentityClient(
            open_id, runner=runner, runtime_store=self.runtime_store
        )
        user = source.resolve_user()
        return self.conversation_store.sync_discovered(
            open_id, source.discover_conversations(user)
        )

    @staticmethod
    def _read_form(environ: dict[str, object]) -> dict[str, list[str]]:
        try:
            length = int(str(environ.get("CONTENT_LENGTH", "0") or "0"))
        except ValueError:
            length = 0
        stream = environ.get("wsgi.input")
        read = getattr(stream, "read", None)
        body = read(length) if callable(read) and length > 0 else b""
        if not isinstance(body, bytes):
            body = b""
        return parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)

    @staticmethod
    def _conversation_page(
        start_response: StartResponse,
        conversations: list[Conversation],
        *,
        saved: bool,
    ) -> Iterable[bytes]:
        def choices(kind: ConversationType, title: str) -> str:
            items = [
                conversation
                for conversation in conversations
                if conversation.conversation_type == kind
            ]
            controls = "".join(
                "<label class='choice'><input type='checkbox' "
                "name='enabled_chat_id' "
                f"value='{html.escape(item.chat_id, quote=True)}'"
                f"{' checked' if item.enabled else ''}>"
                f"<span>{html.escape(item.name)}</span></label>"
                for item in items
            )
            if not controls:
                controls = "<p class='empty'>暂无可选会话</p>"
            return f"<section><h2>{html.escape(title)}</h2>{controls}</section>"

        notice = "<p class='saved'>设置已保存</p>" if saved else ""
        content = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>消息来源设置</title>"
            "<style>body{font-family:system-ui,sans-serif;background:#f5f6f7;"
            "margin:0;padding:32px 20px;color:#1f2329}.panel{max-width:620px;"
            "margin:auto;background:#fff;border-radius:12px;padding:28px;"
            "box-shadow:0 8px 28px #1f232914}h1{font-size:24px;margin:0 0 8px}"
            ".hint,.empty{color:#646a73}section{margin-top:24px}h2{font-size:17px}"
            ".choice{display:flex;gap:10px;align-items:center;padding:10px 0;"
            "border-bottom:1px solid #eff0f1}.choice input{width:18px;height:18px}"
            "button{margin-top:28px;border:0;border-radius:8px;padding:11px 22px;"
            "background:#3370ff;color:#fff;font-size:15px;cursor:pointer}"
            ".saved{padding:10px 12px;background:#e8ffea;color:#237b2c;"
            "border-radius:8px}</style></head><body><main class='panel'>"
            "<h1>消息来源设置</h1>"
            "<p class='hint'>只有你主动勾选的会话会参与每日消息摘要。</p>"
            f"{notice}<form method='post' action='/app/conversations'>"
            f"{choices(ConversationType.GROUP, '群聊')}"
            f"{choices(ConversationType.PRIVATE, '私聊')}"
            "<button type='submit'>保存设置</button></form></main></body></html>"
        ).encode()
        start_response(
            f"{HTTPStatus.OK.value} {HTTPStatus.OK.phrase}",
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(content))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [content]

    def _session_open_id(self, environ: dict[str, object]) -> str | None:
        cookie = SimpleCookie()
        cookie.load(str(environ.get("HTTP_COOKIE", "")))
        morsel = cookie.get("assistant_user")
        if morsel is None:
            return None
        return verify_user_cookie(morsel.value, self.oauth_store.cookie_secret())

    @staticmethod
    def _html(
        start_response: StartResponse,
        status: HTTPStatus,
        title: str,
        detail: str = "",
        *,
        extra_headers: list[tuple[str, str]] | None = None,
        extra_html: str = "",
    ) -> Iterable[bytes]:
        body = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font-family:system-ui,sans-serif;background:#f5f6f7;"
            "margin:0;padding:48px 20px;color:#1f2329}.panel{max-width:560px;"
            "margin:auto;background:#fff;border-radius:12px;padding:32px;"
            "box-shadow:0 8px 28px #1f232914}h1{font-size:24px;margin:0 0 12px}"
            "p{line-height:1.7;color:#646a73;margin:0}</style></head><body>"
            f"<main class='panel'><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(detail)}</p>{extra_html}</main></body></html>"
        ).encode()
        headers = [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ]
        headers.extend(extra_headers or [])
        start_response(f"{status.value} {status.phrase}", headers)
        return [body]

    @staticmethod
    def _redirect(
        start_response: StartResponse,
        location: str,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> Iterable[bytes]:
        headers = [("Location", location), ("Cache-Control", "no-store")]
        headers.extend(extra_headers or [])
        start_response(
            f"{HTTPStatus.SEE_OTHER.value} {HTTPStatus.SEE_OTHER.phrase}",
            headers,
        )
        return [b""]


def serve(app: AssistantWebApp, host: str, port: int) -> None:
    with make_server(
        host,
        port,
        app,
        server_class=ThreadingWSGIServer,
    ) as server:
        logging.getLogger(__name__).info("OAuth web app listening on %s:%s", host, port)
        server.serve_forever()
