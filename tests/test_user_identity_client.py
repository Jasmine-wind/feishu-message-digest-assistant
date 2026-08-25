from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from feishu_assistant.domain import Conversation, ConversationType, User
from feishu_assistant.errors import FeishuAPIError
from feishu_assistant.runtime_store import RuntimeStore
from feishu_assistant.user_identity_client import UserIdentityClient


def test_user_identity_discovers_and_reads_group_and_human_p2p() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str]) -> dict[str, object]:
        commands.append(command)
        joined = " ".join(command)
        if "whoami" in command:
            return {
                "profile": "user-one",
                "identity": "user",
                "onBehalfOf": {"openId": "ou_user", "userName": "刘文涛"},
            }
        if "+chat-list" in command:
            return {
                "ok": True,
                "data": {
                    "chats": [
                        {
                            "chat_id": "oc_group",
                            "name": "研发群",
                            "chat_mode": "group",
                        },
                        {
                            "chat_id": "oc_person",
                            "name": "薛量",
                            "chat_mode": "p2p",
                            "p2p_target_type": "user",
                        },
                        {
                            "chat_id": "oc_bot",
                            "name": "系统助手",
                            "chat_mode": "p2p",
                            "p2p_target_type": "bot",
                        },
                        {
                            "chat_id": "oc_minutes",
                            "name": "智能纪要助手",
                            "chat_mode": "p2p",
                            "p2p_target_type": "bot",
                        },
                    ]
                },
            }
        if "/members" in joined:
            return {
                "ok": True,
                "data": {
                    "items": [
                        {"member_id": "ou_user", "name": "刘文涛"},
                        {"member_id": "ou_other", "name": "薛量"},
                    ]
                },
            }
        if "/open-apis/im/v1/messages" in command:
            return {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "message_id": "om_1",
                            "chat_id": "oc_person",
                            "msg_type": "text",
                            "create_time": "1700000001000",
                            "sender": {
                                "id": "ou_other",
                                "sender_type": "user",
                            },
                            "body": {
                                "content": json.dumps({"text": "我今天修复登录问题"})
                            },
                        }
                    ]
                },
            }
        raise AssertionError(f"unexpected command: {command}")

    client = UserIdentityClient("ou_user", profile="user-one", runner=runner)
    user = client.resolve_user()
    conversations = client.discover_conversations(user)
    messages = client.list_messages("oc_person", 1_700_000_000, 1_700_000_010)

    assert user == User("ou_user", "刘文涛")
    assert [
        (conversation.name, conversation.conversation_type, conversation.enabled)
        for conversation in conversations
    ] == [
        ("研发群", ConversationType.GROUP, False),
        ("薛量", ConversationType.PRIVATE, False),
    ]
    assert len(messages) == 1
    assert messages[0].sender_name == "薛量"
    assert messages[0].text == "我今天修复登录问题"
    assert all(command[1:3] == ["--profile", "user-one"] for command in commands)


def test_user_identity_surfaces_refresh_failure() -> None:
    def runner(_: list[str]) -> dict[str, object]:
        return {
            "ok": False,
            "error": {
                "type": "authorization",
                "message": "refresh token expired; login required",
            },
        }

    client = UserIdentityClient("ou_user", runner=runner)

    with pytest.raises(FeishuAPIError, match="refresh token expired"):
        client.resolve_user()


def test_p2p_name_hint_avoids_member_lookup(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(command: list[str]) -> dict[str, object]:
        commands.append(command)
        if "/open-apis/im/v1/messages" in command:
            return {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "message_id": "om_1",
                            "chat_id": "oc_person",
                            "msg_type": "text",
                            "create_time": "1700000001000",
                            "sender": {"id": "ou_other", "sender_type": "user"},
                            "body": {"content": json.dumps({"text": "进度正常"})},
                        }
                    ]
                },
            }
        raise AssertionError(f"unexpected member lookup: {command}")

    client = UserIdentityClient(
        "ou_user",
        runner=runner,
        runtime_store=RuntimeStore(tmp_path / "runtime.sqlite3"),
    )
    client.set_conversation_context(
        User("ou_user", "刘文涛"),
        [Conversation("oc_person", "薛量", ConversationType.PRIVATE, True)],
    )

    messages = client.list_messages("oc_person", 1_700_000_000, 1_700_000_010)

    assert messages[0].sender_name == "薛量"
    assert not any("/members" in " ".join(command) for command in commands)


def test_persisted_sender_name_avoids_member_lookup_after_restart(
    tmp_path: Path,
) -> None:
    member_calls = 0

    def runner(command: list[str]) -> dict[str, object]:
        nonlocal member_calls
        if "/members" in " ".join(command):
            member_calls += 1
            return {
                "ok": True,
                "data": {"items": [{"member_id": "ou_other", "name": "薛量"}]},
            }
        if "/open-apis/im/v1/messages" in command:
            return {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "message_id": "om_1",
                            "chat_id": "oc_group",
                            "msg_type": "text",
                            "create_time": "1700000001000",
                            "sender": {"id": "ou_other", "sender_type": "user"},
                            "body": {"content": json.dumps({"text": "进度正常"})},
                        }
                    ]
                },
            }
        raise AssertionError(command)

    path = tmp_path / "runtime.sqlite3"
    conversation = Conversation("oc_group", "研发群", ConversationType.GROUP, True)
    for _ in range(2):
        client = UserIdentityClient(
            "ou_user", runner=runner, runtime_store=RuntimeStore(path)
        )
        client.set_conversation_context(User("ou_user", "刘文涛"), [conversation])
        assert client.list_messages("oc_group", 1, 2)[0].sender_name == "薛量"

    assert member_calls == 1


def test_minutes_assistant_chat_id_is_persisted_between_clients(tmp_path: Path) -> None:
    chat_list_calls = 0

    def runner(command: list[str]) -> dict[str, object]:
        nonlocal chat_list_calls
        if "+chat-list" in command:
            chat_list_calls += 1
            return {
                "ok": True,
                "data": {
                    "chats": [
                        {
                            "chat_id": "oc_minutes",
                            "name": "智能纪要助手",
                            "chat_mode": "p2p",
                            "p2p_target_type": "bot",
                        }
                    ]
                },
            }
        if "/open-apis/im/v1/messages" in command:
            return {"ok": True, "data": {"items": []}}
        raise AssertionError(command)

    path = tmp_path / "runtime.sqlite3"
    for _ in range(2):
        client = UserIdentityClient(
            "ou_user", runner=runner, runtime_store=RuntimeStore(path)
        )
        assert client.list_minutes_triggers(1000, 2000) == []

    assert chat_list_calls == 1


def test_user_identity_resolves_and_downloads_real_recording_shape(
    tmp_path: Path,
) -> None:
    def runner(command: list[str]) -> dict[str, object]:
        if "+recording" in command:
            return {
                "ok": True,
                "data": {
                    "recordings": [
                        {
                            "meeting_id": "meeting-1",
                            "duration": "12000",
                            "minute_token": "obcn_token",
                            "recording_url": (
                                "https://meetings.feishu.cn/minutes/obcn_token"
                            ),
                        }
                    ]
                },
            }
        if "+detail" in command:
            return {
                "ok": True,
                "data": {
                    "meetings": [
                        {
                            "meeting_id": "meeting-1",
                            "topic": "项目评审会议",
                            "start_time": "2026-08-18 16:07",
                        }
                    ]
                },
            }
        if "+download" in command:
            return {
                "ok": True,
                "data": {
                    "minute_token": "obcn_token",
                    "download_url": "https://download.test/signed",
                },
            }
        raise AssertionError(f"unexpected command: {command}")

    def download(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://download.test/signed"
        return httpx.Response(
            200,
            headers={
                "content-type": "video/mp4",
                "content-disposition": 'attachment; filename="meeting.mp4"',
            },
            content=b"mp4-bytes",
        )

    http = httpx.Client(transport=httpx.MockTransport(download))
    client = UserIdentityClient("ou_user", runner=runner, http=http)

    recording = client.get_recording("meeting-1")
    media = client.download_recording(recording, tmp_path)

    assert recording.minute_token == "obcn_token"
    assert recording.source_name == "项目评审会议"
    assert recording.source_time == "2026-08-18 16:07"
    assert media.path == (tmp_path / "meeting.mp4").resolve()
    assert media.path.read_bytes() == b"mp4-bytes"
    assert media.content_type == "video/mp4"
    assert media.size_bytes == 9
    http.close()
