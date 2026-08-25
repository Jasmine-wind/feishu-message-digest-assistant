from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .api_usage import APICallRecorder
from .domain import User
from .errors import CheckpointError, ConfigurationError, FeishuAPIError
from .reliability import blocking_file_lock

MESSAGE_DIGEST_OAUTH_SCOPES = (
    "offline_access",
    "auth:user.id:read",
    "im:chat:read",
    "im:chat:readonly",
    "im:chat.members:read",
    "im:message:readonly",
    "im:message.group_msg:get_as_user",
    "im:message.p2p_msg:get_as_user",
    "base:app:read",
    "base:app:create",
    "base:table:read",
    "base:table:create",
    "base:table:delete",
    "base:field:read",
    "base:field:create",
    "base:record:read",
    "base:record:create",
    "base:view:read",
    "base:view:write_only",
)
MEETING_OAUTH_SCOPES = (
    "vc:record:readonly",
    "vc:meeting.meetingid:read",
    "minutes:minutes.media:export",
)
DEFAULT_OAUTH_SCOPES = MESSAGE_DIGEST_OAUTH_SCOPES + MEETING_OAUTH_SCOPES


@dataclass(frozen=True)
class OAuthCredential:
    open_id: str
    name: str
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int


class OAuthStore:
    """Encrypted OAuth credentials and one-time state values in SQLite."""

    def __init__(self, database_path: Path, key_path: Path) -> None:
        self.database_path = database_path
        self.key_path = key_path
        key = self._load_or_create_key()
        self._fernet = Fernet(key)
        self._cookie_secret = hashlib.sha256(key + b"session-cookie").digest()
        self._lock = threading.RLock()
        self._initialize()

    def _load_or_create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            try:
                descriptor = os.open(
                    self.key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = self.key_path.read_bytes().strip()
            else:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(key + b"\n")
                    output.flush()
                    os.fsync(output.fileno())
        try:
            Fernet(key)
        except (ValueError, TypeError) as exc:
            raise CheckpointError("OAuth encryption key is invalid") from exc
        return key

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_users (
                    open_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    access_token BLOB NOT NULL,
                    refresh_token BLOB NOT NULL,
                    access_expires_at INTEGER NOT NULL,
                    refresh_expires_at INTEGER NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS app_access_tokens (
                    app_id TEXT PRIMARY KEY,
                    access_token BLOB NOT NULL,
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(oauth_users)")
            }
            if "initialized" not in columns:
                connection.execute(
                    "ALTER TABLE oauth_users ADD COLUMN initialized INTEGER NOT NULL DEFAULT 0"
                )
        os.chmod(self.database_path, 0o600)

    def issue_state(self, lifetime_seconds: int = 600) -> str:
        state = secrets.token_urlsafe(32)
        digest = hashlib.sha256(state.encode()).hexdigest()
        now = int(time.time())
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM oauth_states WHERE expires_at < ? OR consumed_at IS NOT NULL",
                (now,),
            )
            connection.execute(
                "INSERT INTO oauth_states(state_hash, expires_at) VALUES (?, ?)",
                (digest, now + lifetime_seconds),
            )
        return state

    def consume_state(self, state: str) -> bool:
        digest = hashlib.sha256(state.encode()).hexdigest()
        now = int(time.time())
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE oauth_states
                SET consumed_at = ?
                WHERE state_hash = ? AND consumed_at IS NULL AND expires_at >= ?
                """,
                (now, digest, now),
            )
            return cursor.rowcount == 1

    def upsert(self, credential: OAuthCredential) -> None:
        now = int(time.time())
        access = self._fernet.encrypt(credential.access_token.encode())
        refresh = self._fernet.encrypt(credential.refresh_token.encode())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO oauth_users(
                    open_id, name, access_token, refresh_token,
                    access_expires_at, refresh_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(open_id) DO UPDATE SET
                    name = excluded.name,
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    access_expires_at = excluded.access_expires_at,
                    refresh_expires_at = excluded.refresh_expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    credential.open_id,
                    credential.name,
                    access,
                    refresh,
                    credential.access_expires_at,
                    credential.refresh_expires_at,
                    now,
                    now,
                ),
            )

    def get(self, open_id: str) -> OAuthCredential | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_users WHERE open_id = ?", (open_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            access = self._fernet.decrypt(bytes(row["access_token"])).decode()
            refresh = self._fernet.decrypt(bytes(row["refresh_token"])).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise CheckpointError(
                "stored OAuth credential cannot be decrypted"
            ) from exc
        return OAuthCredential(
            open_id=str(row["open_id"]),
            name=str(row["name"]),
            access_token=access,
            refresh_token=refresh,
            access_expires_at=int(row["access_expires_at"]),
            refresh_expires_at=int(row["refresh_expires_at"]),
        )

    def users(self) -> list[User]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT open_id, name FROM oauth_users
                WHERE initialized = 1 ORDER BY created_at, open_id
                """
            ).fetchall()
        return [User(str(row["open_id"]), str(row["name"])) for row in rows]

    def mark_initialized(self, open_id: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE oauth_users SET initialized = 1, updated_at = ? WHERE open_id = ?",
                (int(time.time()), open_id),
            )
            if cursor.rowcount != 1:
                raise CheckpointError("cannot initialize an unknown OAuth user")

    def is_initialized(self, open_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT initialized FROM oauth_users WHERE open_id = ?", (open_id,)
            ).fetchone()
        return row is not None and int(row["initialized"]) == 1

    def cookie_secret(self) -> bytes:
        return self._cookie_secret

    def get_app_access_token(self, app_id: str) -> tuple[str, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT access_token, expires_at FROM app_access_tokens WHERE app_id = ?",
                (app_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            token = self._fernet.decrypt(bytes(row["access_token"])).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise CheckpointError("stored app access token cannot be decrypted") from exc
        return token, int(row["expires_at"])

    def save_app_access_token(
        self, app_id: str, access_token: str, expires_at: int
    ) -> None:
        encrypted = self._fernet.encrypt(access_token.encode())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_access_tokens(
                    app_id, access_token, expires_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    access_token = excluded.access_token,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (app_id, encrypted, expires_at, int(time.time())),
            )

    def process_lock(self, scope: str) -> Path:
        digest = hashlib.sha256(scope.encode()).hexdigest()[:24]
        return self.database_path.with_name(
            f"{self.database_path.name}.{digest}.lock"
        )


class FeishuOAuthClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        *,
        base_url: str = "https://open.feishu.cn/open-apis",
        scopes: tuple[str, ...] = DEFAULT_OAUTH_SCOPES,
        http: httpx.Client | None = None,
        token_store: OAuthStore | None = None,
        recorder: APICallRecorder | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.base_url = base_url.rstrip("/")
        self.scopes = scopes
        self._http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        self.token_store = token_store
        self.recorder = recorder

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "app_id": self.app_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.scopes),
                "state": state,
            }
        )
        return f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?{query}"

    def exchange_code(self, code: str) -> OAuthCredential:
        app_token = self._app_access_token()
        payload = self._post_user_token(
            "/authen/v1/access_token",
            app_token,
            {"grant_type": "authorization_code", "code": code},
        )
        access_token = self._required_token(payload, "access_token")
        user_info = self.user_info(access_token)
        return self._credential_from_payload(payload, user_info)

    def refresh(self, credential: OAuthCredential) -> OAuthCredential:
        if credential.refresh_expires_at <= int(time.time()):
            raise FeishuAPIError(
                "OAuth refresh token expired; authorization is required"
            )
        app_token = self._app_access_token(credential.open_id)
        payload = self._post_user_token(
            "/authen/v1/refresh_access_token",
            app_token,
            {
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh_token,
            },
            user_open_id=credential.open_id,
        )
        return self._credential_from_payload(
            payload,
            {"open_id": credential.open_id, "name": credential.name},
        )

    def user_info(self, access_token: str) -> dict[str, object]:
        path = "/authen/v1/user_info"
        try:
            response = self._http.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = self._data(response, "OAuth user info")
        except (FeishuAPIError, httpx.HTTPError):
            self._observe("GET", path, "", False)
            raise
        self._observe("GET", path, str(data.get("open_id", "")), True)
        return data

    def _app_access_token(self, user_open_id: str = "") -> str:
        token_store = self.token_store
        cached = (
            token_store.get_app_access_token(self.app_id)
            if token_store is not None
            else None
        )
        if cached is not None and cached[1] > int(time.time()) + 300:
            return cached[0]
        lock_path = (
            token_store.process_lock(f"app-token:{self.app_id}")
            if token_store is not None
            else None
        )
        if lock_path is None:
            return self._request_app_access_token(user_open_id)
        assert token_store is not None
        with blocking_file_lock(lock_path):
            cached = token_store.get_app_access_token(self.app_id)
            if cached is not None and cached[1] > int(time.time()) + 300:
                return cached[0]
            return self._request_app_access_token(user_open_id)

    def _request_app_access_token(self, user_open_id: str) -> str:
        path = "/auth/v3/app_access_token/internal"
        try:
            response = self._http.post(
                f"{self.base_url}{path}",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            payload = self._json(response, "app access token")
        except (FeishuAPIError, httpx.HTTPError):
            self._observe("POST", path, user_open_id, False)
            raise
        self._observe("POST", path, user_open_id, True)
        token = payload.get("app_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuAPIError("app access token response contained no token")
        if self.token_store is not None:
            expires_at = int(time.time()) + self._seconds(payload, "expire", 7200)
            self.token_store.save_app_access_token(self.app_id, token, expires_at)
        return token

    def _post_user_token(
        self,
        path: str,
        app_token: str,
        body: dict[str, str],
        *,
        user_open_id: str = "",
    ) -> dict[str, object]:
        try:
            response = self._http.post(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {app_token}"},
                json=body,
            )
            data = self._data(response, "OAuth token")
        except (FeishuAPIError, httpx.HTTPError):
            self._observe("POST", path, user_open_id, False)
            raise
        self._observe(
            "POST", path, user_open_id or str(data.get("open_id", "")), True
        )
        return data

    def _observe(
        self, method: str, path: str, user_open_id: str, success: bool
    ) -> None:
        if self.recorder is not None:
            self.recorder.observe(method, path, user_open_id, success)

    def _credential_from_payload(
        self, payload: dict[str, object], user_info: dict[str, object]
    ) -> OAuthCredential:
        now = int(time.time())
        open_id = user_info.get("open_id") or payload.get("open_id")
        name = user_info.get("name") or payload.get("name")
        if not isinstance(open_id, str) or not open_id:
            raise FeishuAPIError("OAuth response contained no open_id")
        if not isinstance(name, str) or not name.strip():
            raise FeishuAPIError("OAuth response contained no user name")
        return OAuthCredential(
            open_id=open_id,
            name=name.strip(),
            access_token=self._required_token(payload, "access_token"),
            refresh_token=self._required_token(payload, "refresh_token"),
            access_expires_at=now + self._seconds(payload, "expires_in", 7200),
            refresh_expires_at=now
            + self._seconds(payload, "refresh_expires_in", 2_592_000),
        )

    @staticmethod
    def _required_token(payload: dict[str, object], name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise FeishuAPIError(f"OAuth response contained no {name}")
        return value

    @staticmethod
    def _seconds(payload: dict[str, object], name: str, default: int) -> int:
        try:
            return int(str(payload.get(name, default)))
        except (TypeError, ValueError) as exc:
            raise FeishuAPIError(f"OAuth response contained invalid {name}") from exc

    @staticmethod
    def _json(response: httpx.Response, label: str) -> dict[str, object]:
        try:
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FeishuAPIError(f"{label} request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise FeishuAPIError(f"{label} returned a non-object response")
        code = payload.get("code", 0)
        if isinstance(code, int) and code != 0:
            raise FeishuAPIError(
                f"{label} failed: code={code} msg={payload.get('msg', '')}"
            )
        return payload

    @classmethod
    def _data(cls, response: httpx.Response, label: str) -> dict[str, object]:
        payload = cls._json(response, label)
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise FeishuAPIError(f"{label} returned invalid data")
        return data


class OAuthTokenProvider:
    def __init__(
        self, store: OAuthStore, client: FeishuOAuthClient, open_id: str
    ) -> None:
        self.store = store
        self.client = client
        self.open_id = open_id
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            credential = self.store.get(self.open_id)
            if credential is None:
                raise ConfigurationError(f"OAuth user is not connected: {self.open_id}")
            if credential.access_expires_at > int(time.time()) + 300:
                return credential.access_token
            lock_path = self.store.process_lock(f"user-refresh:{self.open_id}")
            with blocking_file_lock(lock_path):
                credential = self.store.get(self.open_id)
                if credential is None:
                    raise ConfigurationError(
                        f"OAuth user is not connected: {self.open_id}"
                    )
                if credential.access_expires_at <= int(time.time()) + 300:
                    credential = self.client.refresh(credential)
                    if credential.open_id != self.open_id:
                        raise FeishuAPIError(
                            "refreshed OAuth identity changed unexpectedly"
                        )
                    self.store.upsert(credential)
            return credential.access_token


class StoredOAuthRunner:
    """Makes existing UserIdentityClient/BitableClient commands use stored OAuth."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        base_url: str = "https://open.feishu.cn/open-apis",
        http: httpx.Client | None = None,
        recorder: APICallRecorder | None = None,
        user_open_id: str = "",
    ) -> None:
        self.token_provider = token_provider
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=30.0)
        self.recorder = recorder
        self.user_open_id = user_open_id

    def __call__(self, command: list[str]) -> dict[str, object]:
        args = self._arguments(command)
        if args and args[0] == "whoami":
            info = self._request("GET", "/authen/v1/user_info").get("data", {})
            if not isinstance(info, dict):
                return self._error("OAuth user info returned invalid data")
            return {
                "ok": True,
                "onBehalfOf": {
                    "openId": info.get("open_id"),
                    "userName": info.get("name"),
                },
            }
        if args[:2] == ["im", "+chat-list"]:
            payload = self._request_all(
                "GET",
                "/im/v1/chats",
                {
                    "page_size": "100",
                    "sort_type": "ByActiveTimeDesc",
                    "types": "p2p,group",
                    "user_id_type": "open_id",
                },
            )
            data = payload.get("data", {})
            items = data.get("items", []) if isinstance(data, dict) else []
            return {"ok": True, "data": {"chats": items}}
        if args[:2] == ["vc", "+recording"]:
            return self._recordings(args)
        if args[:2] == ["vc", "+detail"]:
            return self._meeting_details(args)
        if args[:2] == ["minutes", "+download"]:
            return self._minutes_download(args)
        if args and args[0] == "api":
            return self._raw_api(args)
        return self._error("unsupported stored-OAuth command")

    @staticmethod
    def _arguments(command: list[str]) -> list[str]:
        args = list(command)
        if args and args[0] == "lark-cli":
            args.pop(0)
        if args[:1] == ["--profile"] and len(args) >= 2:
            del args[:2]
        return args

    def _raw_api(self, args: list[str]) -> dict[str, object]:
        if len(args) < 3:
            return self._error("invalid raw API command")
        method, path = args[1], args[2]
        params = self._json_flag(args, "--params")
        body = self._json_flag(args, "--data")
        if "--page-all" in args:
            return self._request_all(method, path, params)
        return self._request(method, path, params=params, body=body)

    def _recordings(self, args: list[str]) -> dict[str, object]:
        recordings: list[dict[str, object]] = []
        for meeting_id in self._csv_flag(args, "--meeting-ids"):
            payload = self._request("GET", f"/vc/v1/meetings/{meeting_id}/recording")
            data = payload.get("data", {})
            recording = data.get("recording") if isinstance(data, dict) else None
            if not isinstance(recording, dict) and isinstance(data, dict):
                recording = data
            if isinstance(recording, dict):
                item = dict(recording)
                item.setdefault("meeting_id", meeting_id)
                recordings.append(item)
        return {"ok": True, "data": {"recordings": recordings}}

    def _meeting_details(self, args: list[str]) -> dict[str, object]:
        meetings: list[dict[str, object]] = []
        for meeting_id in self._csv_flag(args, "--meeting-ids"):
            payload = self._request("GET", f"/vc/v1/meetings/{meeting_id}")
            data = payload.get("data", {})
            meeting = data.get("meeting") if isinstance(data, dict) else None
            if not isinstance(meeting, dict) and isinstance(data, dict):
                meeting = data
            if isinstance(meeting, dict):
                item = dict(meeting)
                item.setdefault("meeting_id", meeting_id)
                meetings.append(item)
        return {"ok": True, "data": {"meetings": meetings}}

    def _minutes_download(self, args: list[str]) -> dict[str, object]:
        tokens = self._csv_flag(args, "--minute-tokens")
        if len(tokens) != 1:
            return self._error("exactly one minute token is required")
        payload = self._request("GET", f"/minutes/v1/minutes/{tokens[0]}/media")
        data = payload.get("data", {})
        if isinstance(data, dict):
            data.setdefault("minute_token", tokens[0])
        return {"ok": True, "data": data}

    def _request_all(
        self, method: str, path: str, params: dict[str, object]
    ) -> dict[str, object]:
        combined: list[object] = []
        current = dict(params)
        final: dict[str, object] = {}
        for _ in range(100):
            payload = self._request(method, path, params=current)
            data = payload.get("data", {})
            if not isinstance(data, dict):
                return payload
            final = dict(data)
            items = data.get("items", [])
            if isinstance(items, list):
                combined.extend(items)
            if not data.get("has_more"):
                break
            token = data.get("page_token")
            if not isinstance(token, str) or not token:
                break
            current["page_token"] = token
        final["items"] = combined
        final["has_more"] = False
        return {"code": 0, "msg": "success", "data": final}

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        url = f"{self.base_url}{path.removeprefix('/open-apis')}"
        try:
            response = self._http.request(
                method,
                url,
                params=(
                    {key: str(value) for key, value in params.items()}
                    if params is not None
                    else None
                ),
                json=body,
                headers={"Authorization": f"Bearer {self.token_provider()}"},
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if self.recorder is not None:
                self.recorder.observe(
                    method, path, self.user_open_id, False, params
                )
            return self._error(f"OpenAPI request failed: {exc}")
        if not isinstance(payload, dict):
            if self.recorder is not None:
                self.recorder.observe(
                    method, path, self.user_open_id, False, params
                )
            return self._error("OpenAPI returned a non-object response")
        if self.recorder is not None:
            self.recorder.observe(
                method,
                path,
                self.user_open_id,
                payload.get("code", 0) == 0,
                params,
            )
        return payload

    @staticmethod
    def _json_flag(args: list[str], name: str) -> dict[str, object]:
        if name not in args:
            return {}
        try:
            value: Any = json.loads(args[args.index(name) + 1])
        except (IndexError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _csv_flag(args: list[str], name: str) -> list[str]:
        if name not in args:
            return []
        try:
            return [value for value in args[args.index(name) + 1].split(",") if value]
        except IndexError:
            return []

    @staticmethod
    def _error(message: str) -> dict[str, object]:
        logging.getLogger(__name__).debug("stored OAuth runner error: %s", message)
        return {"ok": False, "error": {"type": "api", "message": message}}


def sign_user_cookie(open_id: str, secret: bytes) -> str:
    encoded = base64.urlsafe_b64encode(open_id.encode()).decode().rstrip("=")
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_user_cookie(value: str, secret: bytes) -> str | None:
    try:
        encoded, signature = value.split(".", 1)
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded + padding).decode()
    except (ValueError, UnicodeDecodeError):
        return None
