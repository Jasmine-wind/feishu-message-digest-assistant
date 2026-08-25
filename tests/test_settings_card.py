from __future__ import annotations

import pytest

from feishu_assistant.settings_card import (
    build_settings_entry_card,
    settings_card_idempotency_key,
)


def test_settings_entry_card_has_one_open_url_button() -> None:
    card = build_settings_entry_card("https://assistant.example.com/app")

    assert card["schema"] == "2.0"
    assert card["config"] == {"update_multi": True, "width_mode": "fill"}
    assert card["header"]["title"]["content"] == "工作助手设置"  # type: ignore[index]
    button = card["body"]["elements"][1]  # type: ignore[index]
    assert button["text"]["content"] == "设置消息来源"  # type: ignore[index]
    assert button["behaviors"] == [  # type: ignore[index]
        {
            "type": "open_url",
            "default_url": "https://assistant.example.com/app",
        }
    ]


def test_settings_card_requires_an_absolute_http_url() -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        build_settings_entry_card("/app")


def test_settings_card_idempotency_key_is_stable_and_short() -> None:
    first = settings_card_idempotency_key("ou_user")

    assert first == settings_card_idempotency_key("ou_user")
    assert first != settings_card_idempotency_key("ou_other")
    assert len(first) <= 50
