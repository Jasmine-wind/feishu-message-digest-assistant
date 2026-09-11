from __future__ import annotations

from pathlib import Path

import pytest

from feishu_assistant.domain import User
from feishu_assistant.errors import FeishuAPIError
from feishu_assistant.feishu_client import Recording, RecordingFile
from feishu_assistant.meeting_summary import MeetingMinutes, MeetingSummary
from feishu_assistant.message_digest import DigestTodo


class FakeFeishu:
    def __init__(self) -> None:
        self.sent_cards: list[dict[str, object]] = []
        self.idempotency_keys: list[str] = []

    def get_recording(self, meeting_id: str) -> Recording:
        return Recording(
            meeting_id=meeting_id,
            duration="1000",
            url="https://meetings.feishu.cn/minutes/obcn123",
            minute_token="obcn123",
            source_name="测试用户, 测试同事的视频会议",
            source_time="2026-08-18 16:07:04 GMT+8",
        )

    def download_recording(
        self, recording: Recording, output_dir: Path
    ) -> RecordingFile:
        output_dir.mkdir(parents=True)
        path = output_dir / "meeting.m4a"
        path.write_bytes(b"audio")
        return RecordingFile(path.resolve(), "audio/mp4", 5)

    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str:
        assert target_open_id == "ou_target"
        self.sent_cards.append(card)
        self.idempotency_keys.append(idempotency_key)
        return "om_sent"


class FakeASR:
    def transcribe(self, recording: Path, source_url: str = "") -> str:
        assert recording.read_bytes() == b"audio"
        return "张三负责周五前完成接口。"


class FakeLLM:
    def summarize_meeting(self, transcript: str) -> MeetingMinutes:
        assert transcript == "张三负责周五前完成接口。"
        return MeetingMinutes(
            topic="支付功能项目评审",
            conclusions=("支付功能本周五进入测试",),
            discussions=("对数据库迁移方案存在不同意见",),
            todos=(DigestTodo("完成支付接口", "张三", "本周五"),),
            pending_confirmations=("最终正式上线日期",),
        )


def test_runs_complete_pipeline_and_sends_confirmed_meeting_card(
    tmp_path: Path,
) -> None:
    feishu = FakeFeishu()
    pipeline = MeetingSummary(
        feishu, FakeASR(), FakeLLM(), User("ou_target", "测试用户"), tmp_path
    )

    result = pipeline.run("meeting/unsafe")

    assert result.recording_path == (tmp_path / "meeting_unsafe/meeting.m4a").resolve()
    assert result.transcript_path.read_text(encoding="utf-8") == (
        "张三负责周五前完成接口。\n"
    )
    summary_text = result.summary_path.read_text(encoding="utf-8")
    assert "支付功能项目评审" in summary_text
    assert "会议来源：测试用户, 测试同事的视频会议" in summary_text
    assert "会议时间：2026-08-18 16:07:04 GMT+8" in summary_text
    assert "完成支付接口｜张三｜本周五" in summary_text
    assert result.message_id == "om_sent"

    card = feishu.sent_cards[0]
    assert card["header"] == {
        "template": "blue",
        "title": {"tag": "plain_text", "content": "会议纪要"},
        "subtitle": {"tag": "plain_text", "content": "支付功能项目评审"},
    }
    elements = card["body"]["elements"]  # type: ignore[index]
    contents = [element.get("content", "") for element in elements]  # type: ignore[union-attr]
    assert (
        "📍 **会议来源**\n测试用户, 测试同事的视频会议\n"
        "🕒 **会议时间**\n2026-08-18 16:07:04 GMT+8"
    ) in contents
    assert "💡 **核心结论**\n1. 支付功能本周五进入测试" in contents
    assert "💬 **重要讨论**\n1. 对数据库迁移方案存在不同意见" in contents
    assert "✅ **会议待办**\n完成支付接口｜张三｜本周五" in contents
    assert "❓ **待确认事项**\n1. 最终正式上线日期" in contents
    assert all("我负责" not in content for content in contents)


def test_reuses_asr_and_llm_after_uncertain_push_failure(
    tmp_path: Path,
) -> None:
    class CountingASR:
        def __init__(self) -> None:
            self.calls = 0

        def transcribe(self, recording: Path, source_url: str = "") -> str:
            self.calls += 1
            return "决定今天完成支付接口。"

    class CountingLLM:
        def __init__(self) -> None:
            self.calls = 0

        def summarize_meeting(self, transcript: str) -> MeetingMinutes:
            self.calls += 1
            return MeetingMinutes(
                "支付接口",
                ("今天完成支付接口",),
                (),
                (),
                (),
            )

    class FailPushOnceFeishu(FakeFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = True

        def send_private_card(
            self,
            target_open_id: str,
            card: dict[str, object],
            idempotency_key: str = "",
        ) -> str:
            self.idempotency_keys.append(idempotency_key)
            if self.fail_once:
                self.fail_once = False
                raise FeishuAPIError("uncertain push response")
            self.sent_cards.append(card)
            return "om_recovered"

    feishu = FailPushOnceFeishu()
    asr = CountingASR()
    llm = CountingLLM()
    pipeline = MeetingSummary(
        feishu,
        asr,
        llm,
        User("ou_target", "测试用户"),
        tmp_path,
    )

    with pytest.raises(FeishuAPIError, match="uncertain"):
        pipeline.run("meeting-retry")
    result = pipeline.run("meeting-retry")

    assert result.message_id == "om_recovered"
    assert asr.calls == 1
    assert llm.calls == 1
    assert feishu.idempotency_keys[0] == feishu.idempotency_keys[1]
    assert feishu.idempotency_keys[0].startswith("mt_")


def test_sends_card_for_short_test_content_when_llm_has_no_business_sections(
    tmp_path: Path,
) -> None:
    class TestASR:
        def transcribe(self, recording: Path, source_url: str = "") -> str:
            return "会议功能测试。测试。"

    class EmptyLLM:
        def summarize_meeting(self, transcript: str) -> MeetingMinutes:
            return MeetingMinutes("会议功能测试", (), (), (), ())

    feishu = FakeFeishu()
    pipeline = MeetingSummary(
        feishu,
        TestASR(),
        EmptyLLM(),
        User("ou_target", "测试用户"),
        tmp_path,
    )

    result = pipeline.run("meeting-test")

    assert result.message_id == "om_sent"
    card = feishu.sent_cards[0]
    elements = card["body"]["elements"]  # type: ignore[index]
    contents = [element.get("content", "") for element in elements]  # type: ignore[union-attr]
    assert "💬 **重要讨论**\n1. 会议功能测试" in contents
