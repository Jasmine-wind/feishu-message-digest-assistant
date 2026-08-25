from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from .domain import Conversation, User
from .errors import CheckpointError
from .feishu_client import FeishuMessage


class _MessageReader(Protocol):
    def list_messages(
        self, chat_id: str, start_time: int, end_time: int
    ) -> list[FeishuMessage]: ...


class _MessageSender(Protocol):
    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str: ...


class _LLM(Protocol):
    def summarize_messages(
        self, messages: list[FeishuMessage]
    ) -> MessageDigestSummary | None: ...


class _DigestArchiver(Protocol):
    def archive_message_digest(
        self,
        user: User,
        archive_key: str,
        summary: MessageDigestSummary,
        *,
        start_time: int,
        end_time: int,
        message_count: int,
        source_names: tuple[str, ...],
    ) -> str: ...


@dataclass(frozen=True)
class DigestTodo:
    task: str
    assignee: str
    deadline: str
    source: str = ""
    sender: str = ""


@dataclass(frozen=True)
class DigestEvent:
    content: str
    source: str = ""
    sender: str = ""


@dataclass(frozen=True)
class DigestAttention:
    content: str
    source: str = ""
    sender: str = ""


@dataclass(frozen=True)
class MessageDigestSummary:
    key_events: tuple[DigestEvent, ...]
    todos: tuple[DigestTodo, ...]
    other_attention: tuple[DigestAttention, ...]

    @property
    def has_content(self) -> bool:
        return bool(self.key_events or self.todos or self.other_attention)


@dataclass(frozen=True)
class DigestOutcome:
    start_time: int
    end_time: int
    message_count: int
    sent: bool
    message_id: str = ""
    archive_record_id: str = ""


@dataclass(frozen=True)
class DigestBatch:
    start_time: int
    end_time: int
    idempotency_key: str
    created_at: int
    message_count: int
    card: dict[str, object]
    summary_payload: dict[str, object] | None = None
    source_names: tuple[str, ...] = ()


class MessageCheckpointStore:
    """Durable per-user checkpoint plus one recoverable in-flight batch."""

    def __init__(self, path: Path, scope: str = "default") -> None:
        self.path = path
        self.scope = scope

    def load(self) -> int | None:
        payload = self._read()
        checkpoints = payload.get("checkpoints")
        checkpoint = (
            checkpoints.get(self.scope) if isinstance(checkpoints, dict) else None
        )
        if checkpoint is None and isinstance(
            payload.get("last_message_checkpoint"), int
        ):
            checkpoint = payload["last_message_checkpoint"]
        if checkpoint is None:
            return None
        if not isinstance(checkpoint, int) or checkpoint < 0:
            raise CheckpointError("checkpoint must be a non-negative integer")
        return checkpoint

    def load_batch(self) -> DigestBatch | None:
        payload = self._read()
        batches = payload.get("inflight_batches")
        raw = batches.get(self.scope) if isinstance(batches, dict) else None
        if not isinstance(raw, dict):
            return None
        try:
            card = raw["card"]
            if not isinstance(card, dict):
                raise TypeError("batch card must be an object")
            summary_payload = raw.get("summary_payload")
            if summary_payload is not None and not isinstance(summary_payload, dict):
                raise TypeError("batch summary_payload must be an object")
            raw_source_names = raw.get("source_names", [])
            if not isinstance(raw_source_names, list) or not all(
                isinstance(name, str) for name in raw_source_names
            ):
                raise TypeError("batch source_names must be a list of strings")
            return DigestBatch(
                start_time=int(raw["start_time"]),
                end_time=int(raw["end_time"]),
                idempotency_key=str(raw["idempotency_key"]),
                created_at=int(raw["created_at"]),
                message_count=int(raw["message_count"]),
                card=card,
                summary_payload=summary_payload,
                source_names=tuple(raw_source_names),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError("invalid in-flight message batch") from exc

    def begin_batch(self, batch: DigestBatch) -> None:
        payload = self._read()
        batches = payload.setdefault("inflight_batches", {})
        if not isinstance(batches, dict):
            raise CheckpointError("invalid in-flight message batches")
        batches[self.scope] = {
            "start_time": batch.start_time,
            "end_time": batch.end_time,
            "idempotency_key": batch.idempotency_key,
            "created_at": batch.created_at,
            "message_count": batch.message_count,
            "card": batch.card,
            "summary_payload": batch.summary_payload,
            "source_names": list(batch.source_names),
        }
        self._write(payload)

    def complete_batch(self, checkpoint: int) -> None:
        payload = self._read()
        checkpoints = payload.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            raise CheckpointError("invalid message checkpoints")
        checkpoints[self.scope] = checkpoint
        batches = payload.setdefault("inflight_batches", {})
        if not isinstance(batches, dict):
            raise CheckpointError("invalid in-flight message batches")
        batches.pop(self.scope, None)
        self._write(payload)

    def save(self, checkpoint: int) -> None:
        payload = self._read()
        checkpoints = payload.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            raise CheckpointError("invalid message checkpoints")
        checkpoints[self.scope] = checkpoint
        self._write(payload)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"checkpoints": {}, "inflight_batches": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"invalid message checkpoint {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CheckpointError("message checkpoint must be a JSON object")
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.part")
        try:
            with temporary.open("wb") as output:
                output.write(
                    (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise CheckpointError(
                f"failed to save message checkpoint {self.path}: {exc}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)


def _safe_card_text(value: str) -> str:
    return value.replace("<", "‹").replace(">", "›").strip()


def _numbered(values: tuple[str, ...]) -> str:
    return "\n".join(
        f"{index}. {_safe_card_text(value)}" for index, value in enumerate(values, 1)
    )


def _origin_text(source: str, sender: str) -> str:
    source_text = _safe_card_text(source) or "未明确"
    sender_text = _safe_card_text(sender) or "未明确"
    return f"{source_text} · {sender_text}"


def _event_bullets(values: tuple[DigestEvent, ...]) -> str:
    return "\n".join(
        f"- {_safe_card_text(value.content)}｜"
        f"{_origin_text(value.source, value.sender)}"
        for value in values
    )


def _attention_bullets(values: tuple[DigestAttention, ...]) -> str:
    return "\n".join(
        f"- {_safe_card_text(value.content)}｜"
        f"{_origin_text(value.source, value.sender)}"
        for value in values
    )


def _todo_grid_row(
    *,
    task: str,
    assignee: str,
    deadline: str,
    source: str,
    element_id: str,
    header: bool = False,
) -> dict[str, object]:
    def column(content: str, weight: int) -> dict[str, object]:
        text: dict[str, object]
        if header:
            text = {
                "tag": "markdown",
                "content": f"**{content}**",
                "text_align": "left",
                "text_size": "normal",
            }
        else:
            text = {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": content,
                    "text_align": "left",
                    "text_size": "normal",
                },
            }
        return {
            "tag": "column",
            "width": "weighted",
            "weight": weight,
            "vertical_align": "top",
            "padding": "8px",
            "elements": [text],
        }

    return {
        "tag": "column_set",
        "element_id": element_id,
        "flex_mode": "none",
        "horizontal_spacing": "0px",
        "background_style": "grey" if header else "default",
        "columns": [
            column(task, 3),
            column(assignee, 2),
            column(deadline, 2),
            column(source, 2),
        ],
        "margin": "0px",
    }


def _todo_grid(todos: tuple[DigestTodo, ...]) -> list[dict[str, object]]:
    rows = [
        _todo_grid_row(
            task="事项",
            assignee="负责人",
            deadline="截止时间",
            source="消息出处",
            element_id="digest_todo_header",
            header=True,
        )
    ]
    rows.extend(
        _todo_grid_row(
            task=_safe_card_text(todo.task),
            assignee=_safe_card_text(todo.assignee) or "未明确",
            deadline=_safe_card_text(todo.deadline) or "-",
            source=_origin_text(todo.source, todo.sender),
            element_id=f"digest_todo_{index}",
        )
        for index, todo in enumerate(todos, 1)
    )
    return rows


_DEFAULT_DIGEST_BANNER_IMAGE_KEY = "img_v3_0214h_3a7d0911-84b3-4888-be96-6fbab5178d6g"


def build_digest_card(
    summary: MessageDigestSummary,
    start_time: int,
    end_time: int,
    timezone: ZoneInfo,
    source_names: list[str],
    banner_image_key: str = "",
) -> dict[str, object]:
    start = datetime.fromtimestamp(start_time, timezone).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(end_time, timezone).strftime("%H:%M")
    sources = "、".join(
        dict.fromkeys(_safe_card_text(name) for name in source_names if name.strip())
    )
    image_key = banner_image_key or _DEFAULT_DIGEST_BANNER_IMAGE_KEY
    elements: list[dict[str, object]] = [
        {
            "tag": "column_set",
            "flex_mode": "stretch",
            "horizontal_align": "left",
            "horizontal_spacing": "8px",
            "margin": "0px 0px 0px 0px",
            "columns": [
                {
                    "tag": "column",
                    "width": "auto",
                    "horizontal_align": "left",
                    "vertical_align": "top",
                    "vertical_spacing": "8px",
                    "elements": [],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "horizontal_align": "left",
                    "vertical_align": "top",
                    "vertical_spacing": "8px",
                    "elements": [
                        {
                            "tag": "img",
                            "img_key": image_key,
                            "scale_type": "fit_horizontal",
                            "corner_radius": "8px",
                            "margin": "0px 0px 0px 0px",
                            "transparent": False,
                        }
                    ],
                },
            ],
        },
        {"tag": "hr", "element_id": "digest_header_hr"},
        {
            "tag": "markdown",
            "element_id": "digest_metadata",
            "content": f"时间范围：{start} ～ {end}\n消息来源：{sources or '未明确'}",
            "text_align": "left",
            "text_size": "normal",
            "margin": "0px 0px 0px 0px",
        },
    ]

    if summary.key_events:
        elements.extend(
            [
                {"tag": "hr", "element_id": "digest_key_events_hr"},
                {
                    "tag": "markdown",
                    "element_id": "digest_key_events",
                    "content": (
                        "**<font color='blue'>关键事件</font>**\n"
                        f"{_event_bullets(summary.key_events)}"
                    ),
                    "text_align": "left",
                    "text_size": "normal",
                    "margin": "0px 0px 0px 0px",
                },
            ]
        )
    if summary.todos:
        elements.extend(
            [
                {"tag": "hr", "element_id": "digest_todos_hr"},
                {
                    "tag": "markdown",
                    "element_id": "digest_todos_title",
                    "content": "**<font color='blue'>待办事项</font>**",
                    "text_align": "left",
                    "text_size": "normal",
                    "margin": "0px 0px 0px 0px",
                },
                *_todo_grid(summary.todos),
            ]
        )
    if summary.other_attention:
        elements.extend(
            [
                {"tag": "hr", "element_id": "digest_attention_hr"},
                {
                    "tag": "markdown",
                    "element_id": "digest_attention",
                    "content": (
                        "**<font color='blue'>其他值得关注</font>**\n"
                        f"{_attention_bullets(summary.other_attention)}"
                    ),
                    "text_align": "left",
                    "text_size": "normal",
                    "margin": "0px 0px 0px 0px",
                },
            ]
        )

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "body": {"direction": "vertical", "elements": elements},
    }


class MessageDigest:
    """Read incremental messages, summarize them, send once, then checkpoint."""

    def __init__(
        self,
        reader: _MessageReader,
        sender: _MessageSender,
        llm: _LLM,
        user: User,
        conversations: list[Conversation],
        checkpoint_store: MessageCheckpointStore,
        initial_lookback_seconds: int,
        timezone: ZoneInfo,
        banner_image_key: str = "",
        archiver: _DigestArchiver | None = None,
    ) -> None:
        self.user = user
        self.conversations = [
            conversation for conversation in conversations if conversation.enabled
        ]
        if initial_lookback_seconds < 0:
            raise ValueError("initial_lookback_seconds must not be negative")
        self.reader = reader
        self.sender = sender
        self.llm = llm
        self.checkpoint_store = checkpoint_store
        self.initial_lookback_seconds = initial_lookback_seconds
        self.timezone = timezone
        self.banner_image_key = banner_image_key
        self.archiver = archiver

    def run(self, end_time: int) -> DigestOutcome:
        logger = logging.getLogger(__name__)
        checkpoint = self.checkpoint_store.load()
        batch = self.checkpoint_store.load_batch()
        if batch is not None:
            logger.info(
                "digest user=%s batch=%s stage=recovery status=resumed age_seconds=%s",
                self.user.open_id,
                batch.idempotency_key[-8:],
                max(int(time.time()) - batch.created_at, 0),
            )
            message_id = self.sender.send_private_card(
                self.user.open_id,
                batch.card,
                idempotency_key=batch.idempotency_key,
            )
            archive_record_id = self._archive_batch(batch)
            self.checkpoint_store.complete_batch(batch.end_time)
            logger.info(
                "digest user=%s batch=%s stage=push status=recovered message=%s",
                self.user.open_id,
                batch.idempotency_key[-8:],
                message_id,
            )
            return DigestOutcome(
                start_time=batch.start_time,
                end_time=batch.end_time,
                message_count=batch.message_count,
                sent=True,
                message_id=message_id,
                archive_record_id=archive_record_id,
            )
        start_time = (
            max(0, end_time - self.initial_lookback_seconds)
            if checkpoint is None
            else checkpoint + 1
        )
        if start_time > end_time:
            return DigestOutcome(start_time, end_time, 0, False)
        if not self.conversations:
            return DigestOutcome(start_time, end_time, 0, False)

        by_id: dict[str, FeishuMessage] = {}
        source_names: list[str] = []
        for conversation in self.conversations:
            conversation_messages = self.reader.list_messages(
                conversation.chat_id, start_time, end_time
            )
            if conversation_messages:
                source_names.append(conversation.name)
            for message in conversation_messages:
                by_id.setdefault(
                    message.message_id,
                    replace(message, source_name=conversation.name),
                )
        messages = sorted(
            by_id.values(),
            key=lambda message: (message.create_time, message.message_id),
        )

        if not messages:
            self.checkpoint_store.save(end_time)
            return DigestOutcome(start_time, end_time, 0, False)

        logger.info(
            "digest user=%s batch=%s-%s stage=llm status=started messages=%s",
            self.user.open_id,
            start_time,
            end_time,
            len(messages),
        )
        summary = self.llm.summarize_messages(messages)
        if not summary or not summary.has_content:
            self.checkpoint_store.save(end_time)
            logger.info(
                "digest user=%s batch=%s-%s stage=llm status=skipped reason=no_content",
                self.user.open_id,
                start_time,
                end_time,
            )
            return DigestOutcome(start_time, end_time, len(messages), False)

        card = build_digest_card(
            summary,
            start_time,
            end_time,
            self.timezone,
            source_names,
            self.banner_image_key,
        )
        batch = DigestBatch(
            start_time=start_time,
            end_time=end_time,
            idempotency_key=self._batch_key(start_time, end_time),
            created_at=int(time.time()),
            message_count=len(messages),
            card=card,
            summary_payload=self._summary_payload(summary),
            source_names=tuple(source_names),
        )
        self.checkpoint_store.begin_batch(batch)
        logger.info(
            "digest user=%s batch=%s stage=push status=started",
            self.user.open_id,
            batch.idempotency_key[-8:],
        )
        message_id = self.sender.send_private_card(
            self.user.open_id,
            card,
            idempotency_key=batch.idempotency_key,
        )
        archive_record_id = ""
        if self.archiver is not None:
            logger.info(
                "digest user=%s batch=%s stage=archive status=started",
                self.user.open_id,
                batch.idempotency_key[-8:],
            )
            archive_record_id = self.archiver.archive_message_digest(
                self.user,
                batch.idempotency_key,
                summary,
                start_time=start_time,
                end_time=end_time,
                message_count=len(messages),
                source_names=tuple(source_names),
            )
            logger.info(
                "digest user=%s batch=%s stage=archive status=succeeded record=%s",
                self.user.open_id,
                batch.idempotency_key[-8:],
                archive_record_id,
            )
        self.checkpoint_store.complete_batch(end_time)
        logger.info(
            "digest user=%s batch=%s stage=push status=succeeded message=%s",
            self.user.open_id,
            batch.idempotency_key[-8:],
            message_id,
        )
        return DigestOutcome(
            start_time=start_time,
            end_time=end_time,
            message_count=len(messages),
            sent=True,
            message_id=message_id,
            archive_record_id=archive_record_id,
        )

    def _archive_batch(self, batch: DigestBatch) -> str:
        """Replay the archive for a recovered batch; push dedups by uuid, and
        the archive dedups by archive_key, so this stays idempotent."""
        logger = logging.getLogger(__name__)
        if self.archiver is None or batch.summary_payload is None:
            return ""
        summary = self._load_summary(batch.summary_payload)
        logger.info(
            "digest user=%s batch=%s stage=archive status=restarted",
            self.user.open_id,
            batch.idempotency_key[-8:],
        )
        archive_record_id = self.archiver.archive_message_digest(
            self.user,
            batch.idempotency_key,
            summary,
            start_time=batch.start_time,
            end_time=batch.end_time,
            message_count=batch.message_count,
            source_names=batch.source_names,
        )
        logger.info(
            "digest user=%s batch=%s stage=archive status=succeeded record=%s",
            self.user.open_id,
            batch.idempotency_key[-8:],
            archive_record_id,
        )
        return archive_record_id

    @staticmethod
    def _summary_payload(summary: MessageDigestSummary) -> dict[str, object]:
        return {
            "key_events": [
                {
                    "content": item.content,
                    "source": item.source,
                    "sender": item.sender,
                }
                for item in summary.key_events
            ],
            "todos": [
                {
                    "task": todo.task,
                    "assignee": todo.assignee,
                    "deadline": todo.deadline,
                    "source": todo.source,
                    "sender": todo.sender,
                }
                for todo in summary.todos
            ],
            "other_attention": [
                {
                    "content": item.content,
                    "source": item.source,
                    "sender": item.sender,
                }
                for item in summary.other_attention
            ],
        }

    @staticmethod
    def _load_summary(payload: dict[str, object]) -> MessageDigestSummary:
        try:
            key_events = payload.get("key_events", [])
            todos_raw = payload.get("todos", [])
            other_attention = payload.get("other_attention", [])
            if (
                not isinstance(key_events, list)
                or not isinstance(todos_raw, list)
                or not isinstance(other_attention, list)
            ):
                raise TypeError("summary payload sections must be lists")
            todos = tuple(
                DigestTodo(
                    str(item["task"]),
                    str(item.get("assignee", "未明确")),
                    str(item.get("deadline", "-")),
                    str(item.get("source", "未明确")),
                    str(item.get("sender", "未明确")),
                )
                for item in todos_raw
                if isinstance(item, dict) and item.get("task")
            )
            events = tuple(
                DigestEvent(
                    str(item.get("content", "")),
                    str(item.get("source", "未明确")),
                    str(item.get("sender", "未明确")),
                )
                if isinstance(item, dict)
                else DigestEvent(str(item), "未明确")
                for item in key_events
                if (isinstance(item, dict) and item.get("content"))
                or (not isinstance(item, dict) and str(item).strip())
            )
            attention = tuple(
                DigestAttention(
                    str(item.get("content", "")),
                    str(item.get("source", "未明确")),
                    str(item.get("sender", "未明确")),
                )
                if isinstance(item, dict)
                else DigestAttention(str(item), "未明确")
                for item in other_attention
                if (isinstance(item, dict) and item.get("content"))
                or (not isinstance(item, dict) and str(item).strip())
            )
            return MessageDigestSummary(
                key_events=events,
                todos=todos,
                other_attention=attention,
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise CheckpointError("invalid in-flight message summary") from exc

    def _batch_key(self, start_time: int, end_time: int) -> str:
        digest = hashlib.sha256(
            f"digest:{self.user.open_id}:{start_time}:{end_time}".encode()
        ).hexdigest()
        return f"dg_{digest[:47]}"
