from __future__ import annotations

import hashlib
from urllib.parse import urlparse


def build_settings_entry_card(app_url: str) -> dict[str, object]:
    parsed = urlparse(app_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("settings app URL must be an absolute HTTP(S) URL")
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "工作助手设置"},
            "subtitle": {
                "tag": "plain_text",
                "content": "选择参与摘要的消息来源",
            },
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "在这里随时启用或停用群聊和真人私聊。"
                        "新发现的会话默认不会参与摘要。"
                    ),
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "设置消息来源"},
                    "type": "primary_filled",
                    "width": "fill",
                    "size": "medium",
                    "behaviors": [
                        {"type": "open_url", "default_url": app_url}
                    ],
                },
            ],
        },
    }


def settings_card_idempotency_key(open_id: str) -> str:
    digest = hashlib.sha256(open_id.encode()).hexdigest()[:24]
    return f"settings-entry-{digest}"
