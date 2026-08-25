from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from feishu_assistant.asr_client import ASRClient
from feishu_assistant.config import ProviderConfig
from feishu_assistant.errors import NoEffectiveSpeechError
from feishu_assistant.feishu_client import FeishuMessage
from feishu_assistant.llm_client import LLMClient
from feishu_assistant.meeting_summary import MeetingMinutes
from feishu_assistant.message_digest import (
    DigestAttention,
    DigestEvent,
    DigestTodo,
    MessageDigestSummary,
)

CONFIG = ProviderConfig("key", "model-name", "https://provider.test/v1")


def test_asr_uploads_recording_file(tmp_path: Path) -> None:
    recording = tmp_path / "meeting.m4a"
    recording.write_bytes(b"audio-bytes")
    captured: dict[str, object] = {}

    def create(**kwargs: object) -> object:
        captured.update(kwargs)
        file = kwargs["file"]
        captured["bytes"] = file.read()  # type: ignore[union-attr]
        return SimpleNamespace(text=" transcript ")

    client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create))
    )

    assert ASRClient(CONFIG, client=client).transcribe(recording) == "transcript"
    assert captured["model"] == "model-name"
    assert captured["response_format"] == "json"
    assert captured["bytes"] == b"audio-bytes"


def test_asr_marks_successful_empty_result_as_no_effective_speech(
    tmp_path: Path,
) -> None:
    recording = tmp_path / "meeting.m4a"
    recording.write_bytes(b"audio")
    result = SimpleNamespace(text="   ")
    client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=lambda **_: result))
    )

    with pytest.raises(NoEffectiveSpeechError):
        ASRClient(CONFIG, client=client).transcribe(recording)


def test_asr_polls_dashscope_filetrans_and_downloads_result(
    tmp_path: Path,
) -> None:
    recording = tmp_path / "meeting.mp4"
    recording.write_bytes(b"video")
    task_queries = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_queries
        if request.method == "POST":
            assert request.url.path == "/api/v1/services/audio/asr/transcription"
            body = json.loads(request.content)
            assert body["input"]["file_urls"] == ["https://media.test/signed"]
            assert request.headers["x-dashscope-async"] == "enable"
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if request.url.path == "/api/v1/tasks/task-1":
            task_queries += 1
            if task_queries == 1:
                return httpx.Response(200, json={"output": {"task_status": "RUNNING"}})
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {
                                "subtask_status": "SUCCEEDED",
                                "transcription_url": (
                                    "https://result.test/transcription.json"
                                ),
                            }
                        ],
                    }
                },
            )
        if request.url.host == "result.test":
            return httpx.Response(
                200,
                json={"transcripts": [{"text": " 真实会议转写 "}]},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    config = ProviderConfig(
        "key",
        "qwen-audio-3.0-asr-flash-filetrans",
        "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ASRClient(config, http=http, sleep=lambda _: None)

    assert (
        client.transcribe(recording, source_url="https://media.test/signed")
        == "真实会议转写"
    )
    assert task_queries == 2
    http.close()


def test_llm_uses_transcript_and_returns_summary() -> None:
    captured: dict[str, object] = {}

    def create(**kwargs: object) -> object:
        captured.update(kwargs)
        message = SimpleNamespace(
            content=(
                '{"topic":"项目评审","conclusions":["周五进入测试"],'
                '"discussions":[],"todos":[{"task":"完成接口",'
                '"assignee":"张三","deadline":"周五"}],'
                '"pending_confirmations":[]}'
            )
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    summary = LLMClient(CONFIG, client=client).summarize_meeting("会议转写")

    assert summary == MeetingMinutes(
        topic="项目评审",
        conclusions=("周五进入测试",),
        discussions=(),
        todos=(DigestTodo("完成接口", "张三", "周五"),),
        pending_confirmations=(),
    )
    assert captured["model"] == "model-name"
    assert captured["temperature"] == 0
    assert captured["extra_body"] == {"enable_thinking": False}
    messages = captured["messages"]
    assert "会议转写" in messages[1]["content"]  # type: ignore[index]


def test_llm_summarizes_messages_as_structured_work_items() -> None:
    captured: dict[str, object] = {}

    def create(**kwargs: object) -> object:
        captured.update(kwargs)
        message = SimpleNamespace(
            content=(
                '{"key_events":[{"content":"项目已上线",'
                '"source_message_id":"om_1"}],'
                '"todos":[{"task":"回归测试","assignee":"刘文涛",'
                '"deadline":"今天","source_message_id":"om_1"}],'
                '"other_attention":[{"content":"测试环境需持续关注",'
                '"source_message_id":"om_1"}]}'
            )
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    source = FeishuMessage(
        message_id="om_1",
        chat_id="oc_1",
        msg_type="text",
        create_time=1_700_000_000_000,
        sender_id="ou_1",
        sender_type="user",
        text="项目已上线",
        sender_name="刘文涛",
        source_name="技术开发测试",
    )

    result = LLMClient(CONFIG, client=client).summarize_messages([source])

    assert result == MessageDigestSummary(
        key_events=(DigestEvent("项目已上线", "技术开发测试", "刘文涛"),),
        todos=(
            DigestTodo(
                "回归测试", "刘文涛", "今天", "技术开发测试", "刘文涛"
            ),
        ),
        other_attention=(
            DigestAttention("测试环境需持续关注", "技术开发测试", "刘文涛"),
        ),
    )
    assert captured["temperature"] == 0
    assert captured["extra_body"] == {"enable_thinking": False}
    messages = captured["messages"]
    assert "项目已上线" in messages[1]["content"]  # type: ignore[index]
    assert "source_name=技术开发测试" in messages[1]["content"]  # type: ignore[index]
    assert "source_message_id" in messages[0]["content"]  # type: ignore[index]
    assert "om_1" in messages[1]["content"]  # type: ignore[index]


def test_llm_returns_none_for_no_effective_content() -> None:
    message = SimpleNamespace(
        content='{"key_events":[],"todos":[],"other_attention":[]}'
    )
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )
    source = FeishuMessage("om_1", "oc_1", "text", 1, "ou_1", "user", "收到")

    assert LLMClient(CONFIG, client=client).summarize_messages([source]) is None
