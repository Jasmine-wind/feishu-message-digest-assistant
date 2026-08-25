from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError

DEFAULT_CONFIG_FILE = Path("assistant.toml")


def _settings() -> dict[str, object]:
    path = Path(os.getenv("ASSISTANT_CONFIG_FILE", str(DEFAULT_CONFIG_FILE))).expanduser()
    if not path.exists():
        return {}
    try:
        payload: Any = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"invalid assistant config {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(f"assistant config {path} must be a TOML table")
    return payload


def _setting(section: str, key: str, default: object) -> object:
    current: object = _settings()
    for part in section.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part, {})
    return current.get(key, default) if isinstance(current, dict) else default


def _text(name: str, section: str, key: str, default: str = "") -> str:
    environment = os.getenv(name, "").strip()
    if environment:
        return environment
    value = _setting(section, key, default)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigurationError(f"{section}.{key} must be text or a number")
    return str(value).strip()


def _boolean(
    name: str, section: str, key: str, default: bool
) -> bool:
    environment = os.getenv(name, "").strip().lower()
    if environment:
        if environment in {"1", "true", "yes", "on"}:
            return True
        if environment in {"0", "false", "no", "off"}:
            return False
        raise ConfigurationError(f"{name} must be true or false")
    value = _setting(section, key, default)
    if isinstance(value, bool):
        return value
    raise ConfigurationError(f"{section}.{key} must be true or false")


def _required(name: str, section: str = "", key: str = "") -> str:
    value = (
        _text(name, section, key or name.lower())
        if section
        else os.getenv(name, "").strip()
    )
    if not value:
        location = f"environment variable {name}"
        if section:
            location += f" or {section}.{key}"
        raise ConfigurationError(f"missing required configuration: {location}")
    return value


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    user_access_token: str
    base_url: str = "https://open.feishu.cn/open-apis"

    @classmethod
    def from_env(cls) -> FeishuConfig:
        return cls(
            app_id=_required("FEISHU_APP_ID", "feishu", "app_id"),
            app_secret=_required("FEISHU_APP_SECRET"),
            user_access_token=_required("FEISHU_USER_ACCESS_TOKEN"),
            base_url=_text(
                "FEISHU_BASE_URL",
                "feishu",
                "base_url",
                "https://open.feishu.cn/open-apis",
            ).rstrip("/"),
        )

    @classmethod
    def for_message_digest(cls) -> FeishuConfig:
        """Load the application identity used to read chats and send the digest."""
        return cls(
            app_id=_required("FEISHU_APP_ID", "feishu", "app_id"),
            app_secret=_required("FEISHU_APP_SECRET"),
            user_access_token=os.getenv("FEISHU_USER_ACCESS_TOKEN", "").strip(),
            base_url=_text(
                "FEISHU_BASE_URL",
                "feishu",
                "base_url",
                "https://open.feishu.cn/open-apis",
            ).rstrip("/"),
        )

    @classmethod
    def for_recording_probe(cls) -> FeishuConfig:
        """Load only credentials needed to resolve and download recording media."""
        return cls(
            app_id=_text("FEISHU_APP_ID", "feishu", "app_id"),
            app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            user_access_token=_required("FEISHU_USER_ACCESS_TOKEN"),
            base_url=_text(
                "FEISHU_BASE_URL",
                "feishu",
                "base_url",
                "https://open.feishu.cn/open-apis",
            ).rstrip("/"),
        )


@dataclass(frozen=True)
class ProviderConfig:
    api_key: str
    model: str
    base_url: str
    enable_thinking: bool = False

    @classmethod
    def asr_from_env(cls) -> ProviderConfig:
        return cls(
            api_key=_required("ASR_API_KEY"),
            model=_required("ASR_MODEL", "providers.asr", "model"),
            base_url=_text(
                "ASR_BASE_URL",
                "providers.asr",
                "base_url",
                "https://api.openai.com/v1",
            ).rstrip("/"),
        )

    @classmethod
    def llm_from_env(cls) -> ProviderConfig:
        return cls(
            api_key=_required("LLM_API_KEY"),
            model=_required("LLM_MODEL", "providers.llm", "model"),
            base_url=_text(
                "LLM_BASE_URL",
                "providers.llm",
                "base_url",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ).rstrip("/"),
            enable_thinking=_boolean(
                "LLM_ENABLE_THINKING",
                "providers.llm",
                "enable_thinking",
                False,
            ),
        )


def message_digest_enabled_from_env() -> bool:
    return _boolean(
        "FEATURE_MESSAGE_DIGEST", "features", "message_digest", True
    )


def meeting_summary_enabled_from_env() -> bool:
    return _boolean(
        "FEATURE_MEETING_SUMMARY", "features", "meeting_summary", False
    )


def artifact_dir_from_env() -> Path:
    return Path(_text("ARTIFACT_DIR", "storage", "artifact_dir", "artifacts")).expanduser()


def user_open_ids_from_env() -> list[str]:
    raw = os.getenv("FEISHU_USER_OPEN_IDS", "").strip()
    if raw:
        values: object = raw.split(",")
    else:
        values = _setting("users", "legacy_open_ids", [])
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise ConfigurationError("users.legacy_open_ids must be an array of strings")
    open_ids = list(dict.fromkeys(value.strip() for value in values))
    return [open_id for open_id in open_ids if open_id]


def user_oauth_profile_map_from_env() -> dict[str, str]:
    raw = os.getenv("FEISHU_USER_OAUTH_PROFILES", "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                "FEISHU_USER_OAUTH_PROFILES must be a JSON object"
            ) from exc
    else:
        payload = _setting("users", "legacy_oauth_profiles", {})
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise ConfigurationError(
            "FEISHU_USER_OAUTH_PROFILES must map open_id strings to profile strings"
        )
    return {key.strip(): value.strip() for key, value in payload.items()}


def message_read_identity_from_env() -> str:
    identity = _text(
        "MESSAGE_READ_IDENTITY", "message_digest", "read_identity", "user"
    ).lower() or "user"
    if identity not in {"user", "app"}:
        raise ConfigurationError("MESSAGE_READ_IDENTITY must be user or app")
    return identity


def meeting_trigger_state_file_from_env() -> Path:
    default = artifact_dir_from_env() / "meeting-trigger-state.json"
    return Path(
        _text(
            "MEETING_TRIGGER_STATE_FILE",
            "storage",
            "meeting_trigger_state_file",
            str(default),
        )
    ).expanduser()


def meeting_trigger_initial_lookback_ms_from_env() -> int:
    raw = _text(
        "MEETING_TRIGGER_INITIAL_LOOKBACK_MINUTES",
        "meeting_trigger",
        "initial_lookback_minutes",
        "10",
    ) or "10"
    try:
        minutes = float(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "MEETING_TRIGGER_INITIAL_LOOKBACK_MINUTES must be a number"
        ) from exc
    if minutes < 0:
        raise ConfigurationError(
            "MEETING_TRIGGER_INITIAL_LOOKBACK_MINUTES must not be negative"
        )
    return int(minutes * 60_000)


def meeting_trigger_max_attempts_from_env() -> int:
    raw = _text(
        "MEETING_TRIGGER_MAX_ATTEMPTS",
        "meeting_trigger",
        "max_attempts",
        "5",
    ) or "5"
    try:
        attempts = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "MEETING_TRIGGER_MAX_ATTEMPTS must be an integer"
        ) from exc
    if attempts < 1:
        raise ConfigurationError("MEETING_TRIGGER_MAX_ATTEMPTS must be positive")
    return attempts


def meeting_trigger_poll_seconds_from_env() -> float:
    raw = _text(
        "MEETING_TRIGGER_POLL_SECONDS",
        "meeting_trigger",
        "poll_seconds",
        "600",
    ) or "600"
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "MEETING_TRIGGER_POLL_SECONDS must be a number"
        ) from exc
    if seconds <= 0:
        raise ConfigurationError("MEETING_TRIGGER_POLL_SECONDS must be positive")
    return seconds


def message_retry_attempts_from_env() -> int:
    raw = _text(
        "MESSAGE_RETRY_ATTEMPTS",
        "message_digest",
        "retry_attempts",
        "3",
    ) or "3"
    try:
        attempts = int(raw)
    except ValueError as exc:
        raise ConfigurationError("MESSAGE_RETRY_ATTEMPTS must be an integer") from exc
    if attempts < 1:
        raise ConfigurationError("MESSAGE_RETRY_ATTEMPTS must be positive")
    return attempts


def message_checkpoint_file_from_env() -> Path:
    default = artifact_dir_from_env() / "message-checkpoint.json"
    return Path(
        _text(
            "MESSAGE_CHECKPOINT_FILE",
            "storage",
            "message_checkpoint_file",
            str(default),
        )
    ).expanduser()


def workspace_state_file_from_env() -> Path:
    default = artifact_dir_from_env() / "workspace-state.json"
    return Path(
        _text(
            "WORKSPACE_STATE_FILE",
            "storage",
            "workspace_state_file",
            str(default),
        )
    ).expanduser()


def oauth_database_file_from_env() -> Path:
    default = artifact_dir_from_env() / "oauth-users.sqlite3"
    return Path(
        _text(
            "OAUTH_DATABASE_FILE",
            "oauth",
            "database_file",
            str(default),
        )
    ).expanduser()


def oauth_key_file_from_env() -> Path:
    default = artifact_dir_from_env() / "oauth-token.key"
    return Path(
        _text("OAUTH_KEY_FILE", "oauth", "key_file", str(default))
    ).expanduser()


def oauth_redirect_uri_from_env() -> str:
    return _text(
        "FEISHU_OAUTH_REDIRECT_URI",
        "oauth",
        "redirect_uri",
        "http://127.0.0.1:8765/oauth/callback",
    )


def oauth_public_url_from_env() -> str:
    configured = _text("ASSISTANT_PUBLIC_URL", "oauth", "public_url")
    if configured:
        return configured.rstrip("/")
    redirect_uri = oauth_redirect_uri_from_env()
    suffix = "/oauth/callback"
    return (
        redirect_uri[: -len(suffix)].rstrip("/")
        if redirect_uri.endswith(suffix)
        else redirect_uri.rstrip("/")
    )


def app_entry_url_from_env() -> str:
    return f"{oauth_public_url_from_env()}/app"


def oauth_web_host_from_env() -> str:
    return _text("OAUTH_WEB_HOST", "oauth", "web_host", "127.0.0.1") or "127.0.0.1"


def oauth_web_port_from_env() -> int:
    raw = _text("OAUTH_WEB_PORT", "oauth", "web_port", "8765") or "8765"
    try:
        port = int(raw)
    except ValueError as exc:
        raise ConfigurationError("OAUTH_WEB_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("OAUTH_WEB_PORT must be between 1 and 65535")
    return port


def message_banner_image_key_from_env() -> str:
    return _text(
        "FEISHU_DIGEST_BANNER_IMAGE_KEY",
        "message_digest",
        "banner_image_key",
    )


def message_initial_lookback_seconds_from_env() -> int:
    raw = _text(
        "MESSAGE_INITIAL_LOOKBACK_HOURS",
        "message_digest",
        "initial_lookback_hours",
        "24",
    ) or "24"
    try:
        hours = float(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "MESSAGE_INITIAL_LOOKBACK_HOURS must be a number"
        ) from exc
    if hours < 0:
        raise ConfigurationError("MESSAGE_INITIAL_LOOKBACK_HOURS must not be negative")
    return int(hours * 3600)


def message_timezone_from_env() -> ZoneInfo:
    name = _text(
        "MESSAGE_TIMEZONE", "message_digest", "timezone", "Asia/Shanghai"
    ) or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"unknown MESSAGE_TIMEZONE: {name}") from exc


def message_digest_times_from_env() -> tuple[tuple[int, int], ...]:
    raw = os.getenv("MESSAGE_DIGEST_TIMES", "").strip()
    values: object = (
        [value.strip() for value in raw.split(",") if value.strip()]
        if raw
        else _setting("message_digest", "times", ["08:00", "12:00", "18:00"])
    )
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ConfigurationError(
            "message_digest.times must be an array of HH:MM strings"
        )
    parsed: list[tuple[int, int]] = []
    for value in values:
        parts = value.split(":")
        if len(parts) != 2:
            raise ConfigurationError(f"invalid message digest time: {value}")
        try:
            hour, minute = (int(part) for part in parts)
        except ValueError as exc:
            raise ConfigurationError(f"invalid message digest time: {value}") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ConfigurationError(f"invalid message digest time: {value}")
        parsed.append((hour, minute))
    result = tuple(sorted(set(parsed)))
    if not result:
        raise ConfigurationError("message_digest.times must not be empty")
    return result
