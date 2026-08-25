from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from feishu_assistant.config import FeishuConfig
from feishu_assistant.domain import Conversation, ConversationType, User
from feishu_assistant.errors import CheckpointError, ConfigurationError
from feishu_assistant.feishu_client import FeishuClient, FeishuMessage
from feishu_assistant.message_digest import (
    DigestAttention,
    DigestBatch,
    DigestEvent,
    DigestOutcome,
    DigestTodo,
    MessageCheckpointStore,
    MessageDigest,
    MessageDigestSummary,
)
from feishu_assistant.scheduler import DigestScheduler, next_digest_run


def _config() -> FeishuConfig:
    return FeishuConfig(
        app_id="app-id",
        app_secret="app-secret",
        user_access_token="user-token",
        base_url="https://open.feishu.test/open-apis",
    )


def test_lists_messages_with_member_names_and_pagination() -> None:
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
        if request.url.path.endswith("/im/v1/chats/oc_chat/members"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {"member_id": "ou_sender", "name": "刘文涛"},
                            {"member_id": "ou_other", "name": "薛量"},
                        ],
                    },
                },
            )
        page_token = request.url.params.get("page_token")
        items = [
            {
                "message_id": "om_2" if page_token else "om_1",
                "chat_id": "oc_chat",
                "msg_type": "text",
                "create_time": "1700000002000" if page_token else "1700000001000",
                "sender": {
                    "id": "ou_other" if page_token else "ou_sender",
                    "sender_type": "user",
                },
                "body": {
                    "content": json.dumps(
                        {"text": "第二条" if page_token else "第一条"}
                    )
                },
            }
        ]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "has_more": not bool(page_token),
                    "page_token": "next-page" if not page_token else "",
                    "items": items,
                },
            },
        )

    client = FeishuClient(
        _config(), http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    messages = client.list_messages("oc_chat", 1_700_000_000, 1_700_000_010)

    assert [message.message_id for message in messages] == ["om_1", "om_2"]
    assert [message.text for message in messages] == ["第一条", "第二条"]
    assert [message.sender_name for message in messages] == ["刘文涛", "薛量"]
    assert requests[2].headers["Authorization"] == "Bearer tenant-token"
    assert requests[3].url.params["page_token"] == "next-page"


class FakeFeishu:
    def __init__(
        self, messages: list[FeishuMessage], send_error: Exception | None = None
    ):
        self.messages = messages
        self.send_error = send_error
        self.sent_cards: list[dict[str, object]] = []
        self.idempotency_keys: list[str] = []

    def list_messages(
        self, chat_id: str, start_time: int, end_time: int
    ) -> list[FeishuMessage]:
        return self.messages if chat_id == "oc_chat" else []

    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str:
        assert target_open_id == "ou_target"
        if self.send_error:
            raise self.send_error
        self.sent_cards.append(card)
        self.idempotency_keys.append(idempotency_key)
        return "om_digest"


class FakeLLM:
    def __init__(self, result: MessageDigestSummary | None):
        self.result = result
        self.messages: list[FeishuMessage] = []

    def summarize_messages(
        self, messages: list[FeishuMessage]
    ) -> MessageDigestSummary | None:
        self.messages = messages
        return self.result


def _message() -> FeishuMessage:
    return FeishuMessage(
        message_id="om_new",
        chat_id="oc_chat",
        msg_type="text",
        create_time=1_786_665_600_000,
        sender_id="ou_sender",
        sender_type="user",
        text="请在今天完成支付接口",
        sender_name="薛量",
    )


def _user() -> User:
    return User("ou_target", "刘文涛")


def _conversations() -> list[Conversation]:
    return [
        Conversation("oc_chat", "刘文涛、薛量", ConversationType.GROUP, enabled=True),
        Conversation("oc_empty", "无消息群", ConversationType.GROUP, enabled=True),
    ]


def _summary() -> MessageDigestSummary:
    return MessageDigestSummary(
        key_events=(
            DigestEvent("支付接口要求今天完成", "刘文涛、薛量", "薛量"),
        ),
        todos=(
            DigestTodo(
                "完成支付接口", "刘文涛", "今天", "刘文涛、薛量", "薛量"
            ),
        ),
        other_attention=(
            DigestAttention("下周可能调整测试安排", "刘文涛、薛量", "薛量"),
        ),
    )


def test_digest_sends_confirmed_card_fields_then_advances_checkpoint(
    tmp_path: Path,
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    start = int(datetime(2026, 8, 14, 8, 0, tzinfo=timezone).timestamp())
    end = int(datetime(2026, 8, 14, 12, 0, tzinfo=timezone).timestamp())
    store = MessageCheckpointStore(tmp_path / "checkpoint.json")
    store.save(start - 1)
    feishu = FakeFeishu([_message()])
    llm = FakeLLM(_summary())
    digest = MessageDigest(
        reader=feishu,
        sender=feishu,
        llm=llm,
        user=_user(),
        conversations=_conversations(),
        checkpoint_store=store,
        initial_lookback_seconds=86_400,
        timezone=timezone,
    )

    outcome = digest.run(end_time=end)

    assert outcome == DigestOutcome(start, end, 1, True, "om_digest")
    assert llm.messages[0].source_name == "刘文涛、薛量"
    card = feishu.sent_cards[0]
    assert card["schema"] == "2.0"
    assert card["config"] == {"update_multi": True, "width_mode": "fill"}
    body = card["body"]  # type: ignore[assignment]
    assert body["direction"] == "vertical"  # type: ignore[index]
    elements = body["elements"]  # type: ignore[index]
    banner = elements[0]["columns"][1]["elements"][0]  # type: ignore[index]
    assert banner["img_key"] == ("img_v3_0214h_3a7d0911-84b3-4888-be96-6fbab5178d6g")
    assert banner["corner_radius"] == "8px"
    contents = [element.get("content", "") for element in elements]
    assert "时间范围：2026-08-14 08:00 ～ 12:00\n消息来源：刘文涛、薛量" in contents
    assert (
        "**<font color='blue'>关键事件</font>**\n"
        "- 支付接口要求今天完成｜刘文涛、薛量 · 薛量" in contents
    )
    assert "**<font color='blue'>待办事项</font>**" in contents
    todo_rows = [
        element
        for element in elements
        if str(element.get("element_id", "")).startswith("digest_todo_")
    ]
    assert [row["element_id"] for row in todo_rows] == [
        "digest_todo_header",
        "digest_todo_1",
    ]
    assert todo_rows[1]["columns"][0]["elements"][0]["text"]["content"] == (
        "完成支付接口"
    )
    assert todo_rows[0]["columns"][3]["elements"][0]["content"] == "**消息出处**"
    assert todo_rows[1]["columns"][3]["elements"][0]["text"]["content"] == (
        "刘文涛、薛量 · 薛量"
    )
    assert all(element.get("tag") != "table" for element in elements)
    assert (
        "**<font color='blue'>其他值得关注</font>**\n"
        "- 下周可能调整测试安排｜刘文涛、薛量 · 薛量" in contents
    )
    assert all("需要回复" not in content for content in contents)
    assert all("我负责" not in content for content in contents)
    assert store.load() == end


def test_digest_with_no_enabled_conversation_reads_nothing_and_sends_nothing(
    tmp_path: Path,
) -> None:
    class NoReadFeishu(FakeFeishu):
        def list_messages(
            self, chat_id: str, start_time: int, end_time: int
        ) -> list[FeishuMessage]:
            raise AssertionError("no conversation should be read")

    store = MessageCheckpointStore(tmp_path / "checkpoint.json")
    store.save(100)
    feishu = NoReadFeishu([])
    digest = MessageDigest(
        reader=feishu,
        sender=feishu,
        llm=FakeLLM(_summary()),
        user=_user(),
        conversations=[],
        checkpoint_store=store,
        initial_lookback_seconds=86_400,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    outcome = digest.run(end_time=200)

    assert outcome == DigestOutcome(101, 200, 0, False)
    assert feishu.sent_cards == []
    assert store.load() == 100


def test_digest_todos_show_all_long_items_without_pagination(tmp_path: Path) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    long_todos = tuple(
        DigestTodo(
            f"第 {index} 项：排查并修复排课页面卡顿与数据刷新不及时的问题",
            "刘文涛、薛量" if index % 2 else "未明确",
            "明天下午六点前" if index % 2 else "-",
            "刘文涛、薛量",
            "牛俊泽",
        )
        for index in range(1, 11)
    )
    summary = MessageDigestSummary((), long_todos, ())
    store = MessageCheckpointStore(tmp_path / "checkpoint.json")
    store.save(1_700_000_000)
    feishu = FakeFeishu([_message()])
    digest = MessageDigest(
        reader=feishu,
        sender=feishu,
        llm=FakeLLM(summary),
        user=_user(),
        conversations=_conversations(),
        checkpoint_store=store,
        initial_lookback_seconds=86_400,
        timezone=timezone,
    )

    outcome = digest.run(end_time=1_700_000_010)

    assert outcome.sent is True
    body = cast(dict[str, Any], feishu.sent_cards[0]["body"])
    elements = cast(list[dict[str, Any]], body["elements"])
    todo_rows: list[dict[str, Any]] = [
        element
        for element in elements
        if str(element.get("element_id", "")).startswith("digest_todo_")
    ]
    assert len(todo_rows) == 11
    assert all(element.get("tag") != "table" for element in elements)
    assert "page_size" not in json.dumps(elements, ensure_ascii=False)
    assert [
        row["columns"][0]["elements"][0]["text"]["content"] for row in todo_rows[1:]
    ] == [todo.task for todo in long_todos]


def test_digest_does_not_advance_checkpoint_when_send_fails(tmp_path: Path) -> None:
    store = MessageCheckpointStore(tmp_path / "checkpoint.json")
    store.save(1_700_000_000)
    feishu = FakeFeishu([_message()], RuntimeError("send failed"))
    digest = MessageDigest(
        reader=feishu,
        sender=feishu,
        llm=FakeLLM(_summary()),
        user=_user(),
        conversations=_conversations(),
        checkpoint_store=store,
        initial_lookback_seconds=86_400,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    with pytest.raises(RuntimeError, match="send failed"):
        digest.run(end_time=1_700_000_010)

    assert store.load() == 1_700_000_000
    assert store.load_batch() is not None


def test_digest_advances_without_sending_when_no_effective_content(
    tmp_path: Path,
) -> None:
    store = MessageCheckpointStore(tmp_path / "checkpoint.json")
    feishu = FakeFeishu([_message()])
    digest = MessageDigest(
        reader=feishu,
        sender=feishu,
        llm=FakeLLM(None),
        user=_user(),
        conversations=_conversations(),
        checkpoint_store=store,
        initial_lookback_seconds=100,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    outcome = digest.run(end_time=200)

    assert outcome.sent is False
    assert feishu.sent_cards == []
    assert store.load() == 200


def test_checkpoints_are_independent_per_user(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    first = MessageCheckpointStore(path, scope="ou_first")
    second = MessageCheckpointStore(path, scope="ou_second")

    first.save(100)
    second.save(200)
    first.begin_batch(
        DigestBatch(101, 110, "dg_first", 1_700_000_000, 1, {"schema": "2.0"})
    )

    assert first.load() == 100
    assert second.load() == 200
    assert first.load_batch() is not None
    assert second.load_batch() is None


def test_recovers_inflight_batch_with_same_idempotency_key_after_state_failure(
    tmp_path: Path,
) -> None:
    class FailCompleteOnceStore(MessageCheckpointStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.fail_once = True

        def complete_batch(self, checkpoint: int) -> None:
            if self.fail_once:
                self.fail_once = False
                raise CheckpointError("simulated fsync failure")
            super().complete_batch(checkpoint)

    timezone = ZoneInfo("Asia/Shanghai")
    start = int(datetime(2026, 8, 14, 8, 0, tzinfo=timezone).timestamp())
    end = int(datetime(2026, 8, 14, 12, 0, tzinfo=timezone).timestamp())
    store = FailCompleteOnceStore(tmp_path / "checkpoint.json")
    store.save(start - 1)
    feishu = FakeFeishu([_message()])
    digest = MessageDigest(
        reader=feishu,
        sender=feishu,
        llm=FakeLLM(_summary()),
        user=_user(),
        conversations=_conversations(),
        checkpoint_store=store,
        initial_lookback_seconds=86_400,
        timezone=timezone,
    )

    with pytest.raises(CheckpointError, match="fsync"):
        digest.run(end)
    recovered = digest.run(end + 3600)

    assert recovered.end_time == end
    assert store.load() == end
    assert len(feishu.idempotency_keys) == 2
    assert feishu.idempotency_keys[0] == feishu.idempotency_keys[1]
    assert feishu.idempotency_keys[0].startswith("dg_")


def test_digest_scheduler_stops_on_permanent_checkpoint_error() -> None:
    scheduler = DigestScheduler(
        lambda _: (_ for _ in ()).throw(CheckpointError("corrupt state")),
        ZoneInfo("Asia/Shanghai"),
        sleep=lambda _: None,
    )

    with pytest.raises(CheckpointError, match="corrupt"):
        scheduler.run_forever()


def test_digest_scheduler_stops_after_consecutive_exhausted_failures() -> None:
    calls = 0

    def fail(_: int) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider remains unavailable")

    scheduler = DigestScheduler(
        fail,
        ZoneInfo("Asia/Shanghai"),
        sleep=lambda _: None,
        max_consecutive_failures=2,
    )

    with pytest.raises(ConfigurationError, match="consecutive failures"):
        scheduler.run_forever()
    assert calls == 2


def test_next_digest_run_uses_configured_timezone_and_hours() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    assert next_digest_run(
        datetime(2026, 8, 20, 11, 59, tzinfo=timezone), timezone
    ) == datetime(2026, 8, 20, 12, 0, tzinfo=timezone)
    assert next_digest_run(
        datetime(2026, 8, 20, 18, 0, tzinfo=timezone), timezone
    ) == datetime(2026, 8, 21, 8, 0, tzinfo=timezone)


def test_next_digest_run_supports_configured_minutes() -> None:
    timezone = ZoneInfo("Asia/Shanghai")

    assert next_digest_run(
        datetime(2026, 8, 20, 12, 0, tzinfo=timezone),
        timezone,
        times=((8, 15), (12, 30), (18, 45)),
    ) == datetime(2026, 8, 20, 12, 30, tzinfo=timezone)
