from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from feishu_assistant.config import FeishuConfig
from feishu_assistant.domain import ConversationType, User
from feishu_assistant.errors import FeishuAPIError
from feishu_assistant.feishu_client import FeishuClient


def _config() -> FeishuConfig:
    return FeishuConfig(
        app_id="app-id",
        app_secret="app-secret",
        user_access_token="user-token",
        base_url="https://open.feishu.test/open-apis",
    )


def test_extract_minute_token() -> None:
    assert (
        FeishuClient.extract_minute_token(
            "https://meetings.feishu.cn/minutes/obcnabc123?from=recording"
        )
        == "obcnabc123"
    )
    assert FeishuClient.extract_minute_token("https://example.com/recording") == ""


def test_resolves_recording_url_and_downloads_actual_media(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/vc/v1/meetings/meeting-1/recording"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "recording": {
                            "duration": "60000",
                            "url": "https://meetings.feishu.cn/minutes/obcnmedia123",
                        }
                    },
                },
            )
        if request.url.path.endswith("/minutes/v1/minutes/obcnmedia123/media"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {"download_url": "https://media.test/signed/file"},
                },
            )
        if request.url.host == "media.test":
            return httpx.Response(
                200,
                content=b"actual-mp4-content",
                headers={"Content-Type": "video/mp4"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    client = FeishuClient(_config(), http=http)

    recording = client.get_recording("meeting-1")
    media = client.download_recording(recording, tmp_path)

    assert recording.minute_token == "obcnmedia123"
    assert media.path == (tmp_path / "obcnmedia123.mp4").resolve()
    assert media.path.read_bytes() == b"actual-mp4-content"
    assert media.content_type == "video/mp4"
    assert media.size_bytes == 18
    assert requests[0].headers["Authorization"] == "Bearer user-token"
    assert requests[1].headers["Authorization"] == "Bearer user-token"
    assert "Authorization" not in requests[2].headers


def test_recording_api_error_is_not_hidden_by_http_status() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 124002, "msg": "generating"})

    client = FeishuClient(
        _config(), http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(FeishuAPIError, match="124002"):
        client.get_recording("meeting-1")


def test_sends_private_text_as_bot() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            )
        if request.url.path.endswith("/im/v1/messages"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"message_id": "om_message"},
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = FeishuClient(
        _config(), http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.send_private_text("ou_target", "会议纪要") == "om_message"

    body = json.loads(requests[1].content)
    assert requests[1].url.params["receive_id_type"] == "open_id"
    assert requests[1].headers["Authorization"] == "Bearer tenant-token"
    assert body["receive_id"] == "ou_target"
    assert json.loads(body["content"])["text"] == "会议纪要"


def test_discovers_user_and_enabled_human_conversations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            )
        if request.url.path.endswith("/im/v1/chats"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "chat_id": "oc_group",
                                "chat_mode": "group",
                                "chat_status": "normal",
                                "name": "研发讨论群",
                            },
                            {
                                "chat_id": "oc_person",
                                "chat_mode": "p2p",
                                "chat_status": "normal",
                            },
                            {
                                "chat_id": "oc_bot",
                                "chat_mode": "p2p",
                                "chat_status": "normal",
                            },
                        ],
                    },
                },
            )
        members = {
            "oc_group": [
                {"member_id": "ou_target", "name": "刘文涛"},
                {"member_id": "ou_other", "name": "薛量"},
            ],
            "oc_person": [
                {"member_id": "ou_target", "name": "刘文涛"},
                {"member_id": "ou_other", "name": "薛量"},
            ],
            "oc_bot": [
                {"member_id": "ou_target", "name": "刘文涛"},
                {"member_id": "ou_bot", "name": "系统助手"},
            ],
        }
        chat_id = request.url.path.split("/")[-2]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"has_more": False, "items": members[chat_id]},
            },
        )

    client = FeishuClient(
        _config(), http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    user = client.resolve_user("ou_target")
    conversations = client.discover_conversations(user)

    assert user == User("ou_target", "刘文涛", enabled=True)
    assert [
        (item.name, item.conversation_type, item.enabled) for item in conversations
    ] == [
        ("研发讨论群", ConversationType.GROUP, True),
        ("薛量", ConversationType.PRIVATE, True),
        ("系统助手", ConversationType.PRIVATE, False),
    ]


def test_sends_private_message_card_as_bot() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_card"}})

    client = FeishuClient(
        _config(), http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    card = {"schema": "2.0", "body": {"elements": []}}

    assert (
        client.send_private_card("ou_target", card, idempotency_key="digest-batch-123")
        == "om_card"
    )
    body = json.loads(requests[1].content)
    assert requests[1].url.params["uuid"] == "digest-batch-123"
    assert body["msg_type"] == "interactive"
    assert json.loads(body["content"]) == card
