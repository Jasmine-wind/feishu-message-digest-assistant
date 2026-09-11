from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_assistant.domain import User
from feishu_assistant.errors import (
    ASRError,
    CheckpointError,
    MediaDownloadError,
    NoEffectiveSpeechError,
)
from feishu_assistant.meeting_trigger import MeetingTrigger, MeetingTriggerStateStore
from feishu_assistant.user_identity_client import (
    MinutesTriggerMessage,
    UserIdentityClient,
)


def test_extracts_real_minutes_assistant_recording_card_shape() -> None:
    def runner(command: list[str]) -> dict[str, object]:
        if "+chat-list" in command:
            return {
                "ok": True,
                "data": {
                    "chats": [
                        {
                            "chat_id": "oc_minutes",
                            "name": "智能纪要助手",
                            "chat_mode": "p2p",
                            "p2p_target_type": "bot",
                        },
                        {
                            "chat_id": "oc_system",
                            "name": "系统助手",
                            "chat_mode": "p2p",
                            "p2p_target_type": "bot",
                        },
                    ]
                },
            }
        if "/open-apis/im/v1/messages" in command:
            content = {
                "title": "会议录制已完成",
                "elements": [
                    [
                        {"tag": "text", "text": "主题：项目评审"},
                        {
                            "tag": "text",
                            "text": "日期：2026-08-18 16:07:04 GMT+8",
                        },
                        {"tag": "text", "text": "录制文件（妙记）: "},
                        {
                            "tag": "a",
                            "href": (
                                "https://tenant.feishu.cn/minutes/obcn_new"
                                "?from_source=finish_recording"
                            ),
                            "text": "项目评审",
                        },
                    ]
                ],
            }
            return {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "message_id": "om_new",
                            "create_time": "2000",
                            "msg_type": "interactive",
                            "body": {"content": json.dumps(content)},
                        },
                        {
                            "message_id": "om_text",
                            "create_time": "2100",
                            "msg_type": "text",
                            "body": {"content": '{"text":"ignore"}'},
                        },
                    ]
                },
            }
        raise AssertionError(f"unexpected command: {command}")

    client = UserIdentityClient("ou_user", runner=runner)

    assert client.list_minutes_triggers(1000, 3000) == [
        MinutesTriggerMessage(
            message_id="om_new",
            create_time=2000,
            minute_token="obcn_new",
            url=(
                "https://tenant.feishu.cn/minutes/obcn_new?from_source=finish_recording"
            ),
            source_name="项目评审",
            source_time="2026-08-18 16:07:04 GMT+8",
        )
    ]


class FakeSource:
    def __init__(self, trigger: MinutesTriggerMessage) -> None:
        self.trigger = trigger

    def list_minutes_triggers(
        self, start_time_ms: int, end_time_ms: int
    ) -> list[MinutesTriggerMessage]:
        return [self.trigger]


class FakePipeline:
    def __init__(self, fail_once: bool = False, message_id: str = "om_sent") -> None:
        self.fail_once = fail_once
        self.message_id = message_id
        self.tokens: list[str] = []

    def run_recording(self, recording: object) -> object:
        token = recording.minute_token  # type: ignore[attr-defined]
        self.tokens.append(token)
        if self.fail_once:
            self.fail_once = False
            raise MediaDownloadError("recording is not ready")
        return SimpleNamespace(message_id=self.message_id)


def _trigger_message() -> MinutesTriggerMessage:
    return MinutesTriggerMessage(
        "om_1",
        900,
        "obcn_1",
        "https://tenant.feishu.cn/minutes/obcn_1",
        "项目会议",
    )


def test_processes_each_minute_token_only_once(tmp_path: Path) -> None:
    pipeline = FakePipeline()
    trigger = MeetingTrigger(
        User("ou_user", "测试用户"),
        FakeSource(_trigger_message()),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        MeetingTriggerStateStore(tmp_path / "state.json", "ou_user"),
        initial_lookback_ms=500,
    )

    first = trigger.run(1000)
    second = trigger.run(2000)

    assert first.processed == 1
    assert first.sent == 1
    assert first.suppressed == 0
    assert second.processed == 0
    assert pipeline.tokens == ["obcn_1"]


def test_reports_completed_meeting_suppressed_when_summary_is_empty(
    tmp_path: Path,
) -> None:
    pipeline = FakePipeline(message_id="")
    trigger = MeetingTrigger(
        User("ou_user", "测试用户"),
        FakeSource(_trigger_message()),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        MeetingTriggerStateStore(tmp_path / "state.json", "ou_user"),
        initial_lookback_ms=500,
    )

    result = trigger.run(1000)

    assert result.processed == 1
    assert result.sent == 0
    assert result.suppressed == 1


def test_keeps_not_ready_recording_pending_and_retries_later(tmp_path: Path) -> None:
    pipeline = FakePipeline(fail_once=True)
    trigger = MeetingTrigger(
        User("ou_user", "测试用户"),
        FakeSource(_trigger_message()),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        MeetingTriggerStateStore(tmp_path / "state.json", "ou_user"),
        initial_lookback_ms=500,
        retry_base_ms=100,
    )

    first = trigger.run(1000)
    before_retry = trigger.run(1050)
    restarted = MeetingTrigger(
        User("ou_user", "测试用户"),
        FakeSource(_trigger_message()),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        MeetingTriggerStateStore(tmp_path / "state.json", "ou_user"),
        initial_lookback_ms=500,
        retry_base_ms=100,
    )
    retried = restarted.run(1100)

    assert first.deferred == 1
    assert before_retry.processed == 0
    assert retried.processed == 1
    assert pipeline.tokens == ["obcn_1", "obcn_1"]


def test_recovers_pending_after_successful_push_state_write_failure(
    tmp_path: Path,
) -> None:
    class FailSecondSaveStore(MeetingTriggerStateStore):
        def __init__(self, path: Path, user_open_id: str) -> None:
            super().__init__(path, user_open_id)
            self.saves = 0

        def save(self, state: object) -> None:
            self.saves += 1
            if self.saves == 2:
                raise CheckpointError("simulated state write failure")
            super().save(state)  # type: ignore[arg-type]

    path = tmp_path / "state.json"
    pipeline = FakePipeline()
    trigger = MeetingTrigger(
        User("ou_user", "测试用户"),
        FakeSource(_trigger_message()),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        FailSecondSaveStore(path, "ou_user"),
        initial_lookback_ms=500,
    )

    with pytest.raises(CheckpointError, match="state write"):
        trigger.run(1000)
    restarted = MeetingTrigger(
        User("ou_user", "测试用户"),
        FakeSource(_trigger_message()),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        MeetingTriggerStateStore(path, "ou_user"),
        initial_lookback_ms=500,
    )
    recovered = restarted.run(2000)

    assert recovered.processed == 1
    assert pipeline.tokens == ["obcn_1", "obcn_1"]


def test_skips_successful_asr_with_no_effective_speech(tmp_path: Path) -> None:
    class EmptySpeechPipeline(FakePipeline):
        def run_recording(self, recording: object) -> object:
            self.tokens.append(recording.minute_token)  # type: ignore[attr-defined]
            raise NoEffectiveSpeechError("no speech")

    pipeline = EmptySpeechPipeline()
    trigger = MeetingTrigger(
        User("ou_user", "测试用户"),
        FakeSource(_trigger_message()),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        MeetingTriggerStateStore(tmp_path / "state.json", "ou_user"),
        initial_lookback_ms=500,
    )

    first = trigger.run(1000)
    second = trigger.run(2000)

    assert first.skipped == 1
    assert first.pending == 0
    assert second.processed == 0
    assert pipeline.tokens == ["obcn_1"]


def test_moves_retry_exhausted_recording_to_failed_state(tmp_path: Path) -> None:
    class AlwaysFailPipeline(FakePipeline):
        def run_recording(self, recording: object) -> object:
            self.tokens.append(recording.minute_token)  # type: ignore[attr-defined]
            raise ASRError("provider unavailable")

    pipeline = AlwaysFailPipeline()
    store = MeetingTriggerStateStore(tmp_path / "state.json", "ou_user")
    trigger = MeetingTrigger(
        User("ou_user", "测试用户"),
        FakeSource(_trigger_message()),  # type: ignore[arg-type]
        pipeline,  # type: ignore[arg-type]
        store,
        initial_lookback_ms=500,
        retry_base_ms=100,
        max_attempts=2,
    )

    first = trigger.run(1000)
    exhausted = trigger.run(1100)
    after_failure = trigger.run(2000)
    state = store.load()

    assert first.deferred == 1
    assert exhausted.failed == 1
    assert exhausted.pending == 0
    assert after_failure.processed == 0
    assert pipeline.tokens == ["obcn_1", "obcn_1"]
    assert state is not None
    assert state.failed[0].minute_token == "obcn_1"
    assert state.failed[0].attempts == 2
