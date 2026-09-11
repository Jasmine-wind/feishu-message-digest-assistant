from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import httpx

from feishu_assistant.oauth import (
    FeishuOAuthClient,
    OAuthCredential,
    OAuthStore,
    OAuthTokenProvider,
    StoredOAuthRunner,
    sign_user_cookie,
    verify_user_cookie,
)


def _credential(**changes: object) -> OAuthCredential:
    values: dict[str, object] = {
        "open_id": "ou_user",
        "name": "测试用户",
        "access_token": "u-access-secret",
        "refresh_token": "u-refresh-secret",
        "access_expires_at": int(time.time()) + 3600,
        "refresh_expires_at": int(time.time()) + 86400,
    }
    values.update(changes)
    return OAuthCredential(**values)  # type: ignore[arg-type]


def test_oauth_store_encrypts_tokens_upserts_user_and_consumes_state_once(
    tmp_path: Path,
) -> None:
    store = OAuthStore(tmp_path / "oauth.sqlite3", tmp_path / "oauth.key")
    store.upsert(_credential())
    store.upsert(_credential(name="测试用户（更新）"))

    loaded = store.get("ou_user")
    assert loaded is not None
    assert loaded.name == "测试用户（更新）"
    assert loaded.access_token == "u-access-secret"
    assert store.users() == []
    store.mark_initialized("ou_user")
    assert store.users()[0].open_id == "ou_user"
    raw = (tmp_path / "oauth.sqlite3").read_bytes()
    assert b"u-access-secret" not in raw
    assert b"u-refresh-secret" not in raw
    assert (tmp_path / "oauth.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "oauth.sqlite3").stat().st_mode & 0o777 == 0o600

    state = store.issue_state()
    assert store.consume_state(state) is True
    assert store.consume_state(state) is False


def test_oauth_exchange_and_refresh_use_official_server_endpoints() -> None:
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/auth/v3/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "app"})
        if request.url.path.endswith("/authen/v1/access_token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "user-access",
                        "refresh_token": "user-refresh",
                        "expires_in": 7200,
                        "refresh_expires_in": 2592000,
                    },
                },
            )
        if request.url.path.endswith("/authen/v1/refresh_access_token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "refreshed-access",
                        "refresh_token": "refreshed-refresh",
                        "expires_in": 7200,
                        "refresh_expires_in": 2592000,
                    },
                },
            )
        if request.url.path.endswith("/authen/v1/user_info"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"open_id": "ou_user", "name": "测试用户"}},
            )
        raise AssertionError(request.url)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = FeishuOAuthClient(
        "app-id",
        "app-secret",
        "https://assistant.test/oauth/callback",
        base_url="https://open.test/open-apis",
        http=http,
    )

    exchanged = client.exchange_code("one-time-code")
    refreshed = client.refresh(exchanged)

    assert exchanged.open_id == "ou_user"
    assert refreshed.access_token == "refreshed-access"
    assert (
        "POST",
        "/open-apis/authen/v1/access_token",
        {
            "grant_type": "authorization_code",
            "code": "one-time-code",
        },
    ) in calls
    assert any(path.endswith("/refresh_access_token") for _, path, _ in calls)


def test_token_provider_refreshes_expiring_credential_and_persists_it(
    tmp_path: Path,
) -> None:
    store = OAuthStore(tmp_path / "oauth.sqlite3", tmp_path / "oauth.key")
    store.upsert(_credential(access_expires_at=int(time.time()) + 10))

    class RefreshClient:
        def refresh(self, credential: OAuthCredential) -> OAuthCredential:
            return _credential(
                access_token="new-access",
                refresh_token="new-refresh",
                access_expires_at=int(time.time()) + 7200,
            )

    provider = OAuthTokenProvider(
        store,
        RefreshClient(),  # type: ignore[arg-type]
        "ou_user",
    )

    assert provider() == "new-access"
    assert store.get("ou_user") == _credential(
        access_token="new-access",
        refresh_token="new-refresh",
        access_expires_at=store.get("ou_user").access_expires_at,  # type: ignore[union-attr]
    )


def test_refresh_reuses_cached_app_token_and_stored_identity(tmp_path: Path) -> None:
    store = OAuthStore(tmp_path / "oauth.sqlite3", tmp_path / "oauth.key")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/v3/app_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "app_access_token": "app", "expire": 7200},
            )
        if request.url.path.endswith("/authen/v1/refresh_access_token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 7200,
                        "refresh_expires_in": 2592000,
                    },
                },
            )
        raise AssertionError(request.url)

    client = FeishuOAuthClient(
        "app-id",
        "app-secret",
        "https://assistant.test/oauth/callback",
        base_url="https://open.test/open-apis",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        token_store=store,
    )

    first = client.refresh(_credential())
    client.refresh(first)

    assert first.open_id == "ou_user"
    assert first.name == "测试用户"
    assert calls.count("/open-apis/auth/v3/app_access_token/internal") == 1
    assert calls.count("/open-apis/authen/v1/refresh_access_token") == 2
    assert not any(path.endswith("/user_info") for path in calls)


def test_second_provider_rechecks_database_after_refresh_lock(tmp_path: Path) -> None:
    store = OAuthStore(tmp_path / "oauth.sqlite3", tmp_path / "oauth.key")
    store.upsert(_credential(access_expires_at=int(time.time()) + 10))
    refreshes = 0

    class RefreshClient:
        def refresh(self, credential: OAuthCredential) -> OAuthCredential:
            nonlocal refreshes
            refreshes += 1
            return _credential(
                access_token="shared-access",
                refresh_token="shared-refresh",
                access_expires_at=int(time.time()) + 7200,
            )

    first = OAuthTokenProvider(store, RefreshClient(), "ou_user")  # type: ignore[arg-type]
    second = OAuthTokenProvider(store, RefreshClient(), "ou_user")  # type: ignore[arg-type]

    assert first() == "shared-access"
    assert second() == "shared-access"
    assert refreshes == 1


def test_stored_oauth_runner_reuses_existing_user_identity_command_shape() -> None:
    pages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        if request.url.path.endswith("/authen/v1/user_info"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"open_id": "ou_user", "name": "测试用户"}},
            )
        if request.url.path.endswith("/im/v1/chats"):
            pages += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"chat_id": f"oc_{pages}"}],
                        "has_more": pages == 1,
                        "page_token": "next" if pages == 1 else "",
                    },
                },
            )
        raise AssertionError(request.url)

    runner = StoredOAuthRunner(
        lambda: "user-token",
        base_url="https://open.test/open-apis",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    identity = runner(["lark-cli", "whoami", "--as", "user"])
    chats = runner(["lark-cli", "im", "+chat-list", "--as", "user", "--page-all"])

    assert identity["onBehalfOf"] == {
        "openId": "ou_user",
        "userName": "测试用户",
    }
    assert chats["data"] == {"chats": [{"chat_id": "oc_1"}, {"chat_id": "oc_2"}]}


def test_user_cookie_is_signed_and_tamper_evident() -> None:
    secret = b"x" * 32
    cookie = sign_user_cookie("ou_user", secret)
    assert verify_user_cookie(cookie, secret) == "ou_user"
    assert verify_user_cookie(cookie + "tampered", secret) is None


def test_oauth_database_has_no_duplicate_user_rows(tmp_path: Path) -> None:
    path = tmp_path / "oauth.sqlite3"
    store = OAuthStore(path, tmp_path / "oauth.key")
    store.upsert(_credential())
    store.upsert(_credential(access_token="second"))
    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM oauth_users").fetchone()[0]
    assert count == 1
