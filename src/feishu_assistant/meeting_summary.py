from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .domain import User
from .feishu_client import Recording, RecordingFile
from .message_digest import DigestTodo, _numbered, _safe_card_text


@dataclass(frozen=True)
class MeetingMinutes:
    topic: str
    conclusions: tuple[str, ...]
    discussions: tuple[str, ...]
    todos: tuple[DigestTodo, ...]
    pending_confirmations: tuple[str, ...]

    @property
    def has_business_content(self) -> bool:
        return bool(
            self.conclusions
            or self.discussions
            or self.todos
            or self.pending_confirmations
        )


class _RecordingSource(Protocol):
    def get_recording(self, meeting_id: str) -> Recording: ...

    def download_recording(
        self, recording: Recording, output_dir: Path
    ) -> RecordingFile: ...


class _CardSender(Protocol):
    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str: ...


class _Feishu(_RecordingSource, _CardSender, Protocol):
    pass


class _ASR(Protocol):
    def transcribe(self, recording: Path, source_url: str = "") -> str: ...


class _LLM(Protocol):
    def summarize_meeting(self, transcript: str) -> MeetingMinutes: ...


class _MinutesArchiver(Protocol):
    def archive_meeting_minutes(
        self,
        user: User,
        archive_key: str,
        minutes: MeetingMinutes,
        *,
        recording: Recording,
        message_id: str = "",
    ) -> str: ...


@dataclass(frozen=True)
class MeetingSummaryResult:
    meeting_id: str
    recording_path: Path
    transcript_path: Path
    summary_path: Path
    message_id: str
    archive_record_id: str = ""


def build_meeting_card(
    minutes: MeetingMinutes,
    source_name: str = "",
    source_time: str = "",
) -> dict[str, object]:
    elements: list[dict[str, object]] = []
    source_lines: list[str] = []
    if source_name.strip():
        source_lines.append(f"📍 **会议来源**\n{_safe_card_text(source_name)}")
    if source_time.strip():
        source_lines.append(f"🕒 **会议时间**\n{_safe_card_text(source_time)}")
    if source_lines:
        elements.append({"tag": "markdown", "content": "\n".join(source_lines)})
    sections: list[str] = []
    if minutes.conclusions:
        sections.append(f"💡 **核心结论**\n{_numbered(minutes.conclusions)}")
    if minutes.discussions:
        sections.append(f"💬 **重要讨论**\n{_numbered(minutes.discussions)}")
    if minutes.todos:
        todo_lines = "\n".join(
            f"{_safe_card_text(todo.task)}｜"
            f"{_safe_card_text(todo.assignee) or '未明确'}｜"
            f"{_safe_card_text(todo.deadline) or '-'}"
            for todo in minutes.todos
        )
        sections.append(f"✅ **会议待办**\n{todo_lines}")
    if minutes.pending_confirmations:
        sections.append(
            f"❓ **待确认事项**\n{_numbered(minutes.pending_confirmations)}"
        )
    for content in sections:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": content})
    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "会议纪要"},
            "subtitle": {
                "tag": "plain_text",
                "content": _safe_card_text(minutes.topic) or "未命名会议",
            },
        },
        "body": {"elements": elements},
    }


def render_meeting_minutes(
    minutes: MeetingMinutes,
    source_name: str = "",
    source_time: str = "",
) -> str:
    lines = ["会议纪要", minutes.topic or "未命名会议"]
    if source_name.strip():
        lines.append(f"会议来源：{source_name.strip()}")
    if source_time.strip():
        lines.append(f"会议时间：{source_time.strip()}")
    sections: list[tuple[str, tuple[str, ...]]] = [
        ("核心结论", minutes.conclusions),
        ("重要讨论", minutes.discussions),
        (
            "会议待办",
            tuple(
                f"{todo.task}｜{todo.assignee or '未明确'}｜{todo.deadline or '-'}"
                for todo in minutes.todos
            ),
        ),
        ("待确认事项", minutes.pending_confirmations),
    ]
    for heading, values in sections:
        if values:
            lines.extend(["", heading, *values])
    return "\n".join(lines)


class MeetingSummary:
    """The complete Task 1 vertical pipeline for one known meeting_id."""

    def __init__(
        self,
        feishu: _Feishu,
        asr: _ASR,
        llm: _LLM,
        user: User,
        artifact_dir: Path,
        recording_source: _RecordingSource | None = None,
        archiver: _MinutesArchiver | None = None,
    ) -> None:
        self.feishu = feishu
        self.recording_source = recording_source or feishu
        self.asr = asr
        self.llm = llm
        self.user = user
        self.artifact_dir = artifact_dir
        self.archiver = archiver

    def run(self, meeting_id: str) -> MeetingSummaryResult:
        return self.run_recording(self.recording_source.get_recording(meeting_id))

    def run_recording(self, recording: Recording) -> MeetingSummaryResult:
        meeting_id = recording.meeting_id
        token_label = recording.minute_token[-6:]
        meeting_dir = self.artifact_dir / self._safe_directory_name(meeting_id)
        transcript_path = meeting_dir / "transcript.txt"
        minutes_path = meeting_dir / "minutes.json"
        media_path_file = meeting_dir / "recording-path.txt"
        logger = logging.getLogger(__name__)

        if transcript_path.is_file():
            transcript = transcript_path.read_text(encoding="utf-8").strip()
            if not transcript:
                raise ValueError("cached meeting transcript is empty")
            recording_path = self._cached_recording_path(media_path_file, meeting_dir)
            logger.info(
                "meeting user=%s minute=%s stage=asr status=reused",
                self.user.open_id,
                token_label,
            )
        else:
            logger.info(
                "meeting user=%s minute=%s stage=download status=started",
                self.user.open_id,
                token_label,
            )
            media = self.recording_source.download_recording(recording, meeting_dir)
            recording_path = media.path
            self._write_text(media_path_file, str(media.path))
            logger.info(
                "meeting user=%s minute=%s stage=download status=succeeded bytes=%s",
                self.user.open_id,
                token_label,
                media.size_bytes,
            )
            logger.info(
                "meeting user=%s minute=%s stage=asr status=started",
                self.user.open_id,
                token_label,
            )
            transcript = self.asr.transcribe(media.path, source_url=media.source_url)
            self._write_text(transcript_path, transcript)
            logger.info(
                "meeting user=%s minute=%s stage=asr status=succeeded chars=%s",
                self.user.open_id,
                token_label,
                len(transcript),
            )

        if minutes_path.is_file():
            minutes = self._load_minutes(minutes_path)
            logger.info(
                "meeting user=%s minute=%s stage=llm status=reused",
                self.user.open_id,
                token_label,
            )
        else:
            logger.info(
                "meeting user=%s minute=%s stage=llm status=started",
                self.user.open_id,
                token_label,
            )
            minutes = self._ensure_content(
                minutes=self.llm.summarize_meeting(transcript),
                transcript=transcript,
            )
            self._write_json(minutes_path, self._minutes_payload(minutes))
            logger.info(
                "meeting user=%s minute=%s stage=llm status=succeeded",
                self.user.open_id,
                token_label,
            )

        summary_path = meeting_dir / "summary.txt"
        self._write_text(
            summary_path,
            render_meeting_minutes(
                minutes,
                recording.source_name,
                recording.source_time,
            ),
        )

        message_id = ""
        archive_record_id = ""
        if minutes.has_business_content:
            idempotency_key = self._idempotency_key(recording.minute_token)
            logger.info(
                "meeting user=%s minute=%s stage=push status=started",
                self.user.open_id,
                token_label,
            )
            message_id = self.feishu.send_private_card(
                self.user.open_id,
                build_meeting_card(
                    minutes,
                    recording.source_name,
                    recording.source_time,
                ),
                idempotency_key=idempotency_key,
            )
            logger.info(
                "meeting user=%s minute=%s stage=push status=succeeded message=%s",
                self.user.open_id,
                token_label,
                message_id,
            )
            if self.archiver is not None:
                logger.info(
                    "meeting user=%s minute=%s stage=archive status=started",
                    self.user.open_id,
                    token_label,
                )
                archive_record_id = self.archiver.archive_meeting_minutes(
                    self.user,
                    idempotency_key,
                    minutes,
                    recording=recording,
                    message_id=message_id,
                )
                logger.info(
                    "meeting user=%s minute=%s stage=archive status=succeeded "
                    "record=%s",
                    self.user.open_id,
                    token_label,
                    archive_record_id,
                )
        return MeetingSummaryResult(
            meeting_id=meeting_id,
            recording_path=recording_path,
            transcript_path=transcript_path.resolve(),
            summary_path=summary_path.resolve(),
            message_id=message_id,
            archive_record_id=archive_record_id,
        )

    def _idempotency_key(self, minute_token: str) -> str:
        digest = hashlib.sha256(
            f"meeting:{self.user.open_id}:{minute_token}".encode()
        ).hexdigest()
        return f"mt_{digest[:47]}"

    @staticmethod
    def _minutes_payload(minutes: MeetingMinutes) -> dict[str, object]:
        return {
            "topic": minutes.topic,
            "conclusions": list(minutes.conclusions),
            "discussions": list(minutes.discussions),
            "todos": [
                {
                    "task": todo.task,
                    "assignee": todo.assignee,
                    "deadline": todo.deadline,
                }
                for todo in minutes.todos
            ],
            "pending_confirmations": list(minutes.pending_confirmations),
        }

    @staticmethod
    def _load_minutes(path: Path) -> MeetingMinutes:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("minutes must be an object")
            todos = tuple(
                DigestTodo(
                    str(item["task"]),
                    str(item.get("assignee", "未明确")),
                    str(item.get("deadline", "-")),
                )
                for item in payload.get("todos", [])
                if isinstance(item, dict) and item.get("task")
            )
            return MeetingMinutes(
                topic=str(payload.get("topic", "")),
                conclusions=tuple(str(item) for item in payload.get("conclusions", [])),
                discussions=tuple(str(item) for item in payload.get("discussions", [])),
                todos=todos,
                pending_confirmations=tuple(
                    str(item) for item in payload.get("pending_confirmations", [])
                ),
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"invalid cached meeting minutes: {path}") from exc

    @staticmethod
    def _cached_recording_path(path_file: Path, meeting_dir: Path) -> Path:
        if path_file.is_file():
            raw = path_file.read_text(encoding="utf-8").strip()
            if raw:
                return Path(raw)
        ignored = {
            "transcript.txt",
            "summary.txt",
            "minutes.json",
            "recording-path.txt",
        }
        candidate = next(
            (
                path
                for path in meeting_dir.iterdir()
                if path.is_file() and path.name not in ignored
            ),
            meeting_dir,
        )
        return candidate.resolve()

    @staticmethod
    def _ensure_content(minutes: MeetingMinutes, transcript: str) -> MeetingMinutes:
        if minutes.has_business_content:
            return minutes
        normalized = " ".join(transcript.split()).strip()
        fallback = minutes.topic.strip()
        if fallback in {"", "未明确", "未命名会议"}:
            fallback = normalized
        if len(fallback) > 500:
            fallback = f"{fallback[:497]}..."
        if not fallback:
            return minutes
        return MeetingMinutes(
            topic=minutes.topic,
            conclusions=minutes.conclusions,
            discussions=(fallback,),
            todos=minutes.todos,
            pending_confirmations=minutes.pending_confirmations,
        )

    @staticmethod
    def _safe_directory_name(meeting_id: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", meeting_id.strip())
        if cleaned in {"", ".", ".."}:
            raise ValueError("meeting_id does not contain a safe path component")
        return cleaned

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        MeetingSummary._write_bytes(path, (content + "\n").encode())

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        MeetingSummary._write_bytes(
            path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
        )

    @staticmethod
    def _write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.part")
        try:
            with temporary.open("wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
