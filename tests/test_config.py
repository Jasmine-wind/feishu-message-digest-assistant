from __future__ import annotations

from pathlib import Path

import pytest

from feishu_assistant.config import (
    ProviderConfig,
    app_entry_url_from_env,
    meeting_summary_enabled_from_env,
    message_digest_enabled_from_env,
    message_digest_times_from_env,
    message_retry_attempts_from_env,
    message_timezone_from_env,
    oauth_web_port_from_env,
    user_open_ids_from_env,
)
from feishu_assistant.errors import ConfigurationError


def _write_config(path: Path, digest_times: str = '["08:15", "12:30"]') -> None:
    path.write_text(
        f"""
[users]
legacy_open_ids = ["ou_config_a", "ou_config_b"]

[message_digest]
times = {digest_times}
timezone = "Asia/Shanghai"
retry_attempts = 4

[oauth]
web_port = 9876
public_url = "https://assistant.example.com/root/"

[providers.llm]
model = "configured-model"
base_url = "https://llm.example/v1"
""".strip(),
        encoding="utf-8",
    )


def test_non_secret_settings_are_loaded_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "custom.toml"
    _write_config(config_file)
    monkeypatch.setenv("ASSISTANT_CONFIG_FILE", str(config_file))
    for name in (
        "FEISHU_USER_OPEN_IDS",
        "MESSAGE_DIGEST_TIMES",
        "MESSAGE_TIMEZONE",
        "MESSAGE_RETRY_ATTEMPTS",
        "OAUTH_WEB_PORT",
        "ASSISTANT_PUBLIC_URL",
        "LLM_MODEL",
        "LLM_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "secret-from-environment")

    provider = ProviderConfig.llm_from_env()

    assert user_open_ids_from_env() == ["ou_config_a", "ou_config_b"]
    assert message_digest_times_from_env() == ((8, 15), (12, 30))
    assert message_timezone_from_env().key == "Asia/Shanghai"
    assert message_retry_attempts_from_env() == 4
    assert oauth_web_port_from_env() == 9876
    assert app_entry_url_from_env() == "https://assistant.example.com/root/app"
    assert provider.api_key == "secret-from-environment"
    assert provider.model == "configured-model"
    assert provider.base_url == "https://llm.example/v1"


def test_environment_can_override_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "custom.toml"
    _write_config(config_file)
    monkeypatch.setenv("ASSISTANT_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("FEISHU_USER_OPEN_IDS", "ou_env")
    monkeypatch.setenv("MESSAGE_DIGEST_TIMES", "09:05,17:45")

    assert user_open_ids_from_env() == ["ou_env"]
    assert message_digest_times_from_env() == ((9, 5), (17, 45))


def test_features_default_to_message_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASSISTANT_CONFIG_FILE", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("FEATURE_MESSAGE_DIGEST", raising=False)
    monkeypatch.delenv("FEATURE_MEETING_SUMMARY", raising=False)

    assert message_digest_enabled_from_env() is True
    assert meeting_summary_enabled_from_env() is False


def test_feature_flags_can_be_overridden_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "features.toml"
    config_file.write_text(
        "[features]\nmessage_digest = false\nmeeting_summary = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASSISTANT_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("FEATURE_MESSAGE_DIGEST", "true")
    monkeypatch.setenv("FEATURE_MEETING_SUMMARY", "false")

    assert message_digest_enabled_from_env() is True
    assert meeting_summary_enabled_from_env() is False


def test_invalid_feature_flag_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASSISTANT_CONFIG_FILE", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("FEATURE_MEETING_SUMMARY", "sometimes")

    with pytest.raises(ConfigurationError, match="FEATURE_MEETING_SUMMARY"):
        meeting_summary_enabled_from_env()


def test_invalid_digest_time_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "custom.toml"
    _write_config(config_file, digest_times='["24:00"]')
    monkeypatch.setenv("ASSISTANT_CONFIG_FILE", str(config_file))
    monkeypatch.delenv("MESSAGE_DIGEST_TIMES", raising=False)

    with pytest.raises(ConfigurationError, match="invalid message digest time"):
        message_digest_times_from_env()
