from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .domain import User
from .errors import (
    AssistantError,
    CheckpointError,
    ConfigurationError,
    NoEffectiveSpeechError,
)
from .feishu_client import Recording
from .meeting_summary import MeetingSummary
from .user_identity_client import MinutesTriggerMessage, UserIdentityClient


@dataclass(frozen=True)
class PendingRecording:
    minute_token: str
    url: str
    source_name: str
    source_time: str
    message_id: str
    attempts: int = 0
    next_attempt_at_ms: int = 0


@dataclass(frozen=True)
class FailedRecording:
    minute_token: str
    message_id: str
    attempts: int
    failed_at_ms: int
    reason: str


@dataclass(frozen=True)
class MeetingTriggerState:
    cursor_ms: int
    processed_tokens: tuple[str, ...]
    skipped_tokens: tuple[str, ...]
    failed: tuple[FailedRecording, ...]
    pending: tuple[PendingRecording, ...]


@dataclass(frozen=True)
class MeetingTriggerResult:
    user_open_id: str
    discovered: int
    processed: int
    sent: int
    suppressed: int
    skipped: int
    deferred: int
    failed: int
    pending: int


class MeetingTriggerStateStore:
    def __init__(self, path: Path, user_open_id: str) -> None:
        self.path = path
        self.user_open_id = user_open_id

    def load(self) -> MeetingTriggerState | None:
        root = self._read_root()
        users = root.get("users", {})
        raw = users.get(self.user_open_id) if isinstance(users, dict) else None
        if not isinstance(raw, dict):
            return None
        try:
            pending = tuple(
                PendingRecording(
                    minute_token=str(item["minute_token"]),
                    url=str(item["url"]),
                    source_name=str(item["source_name"]),
                    source_time=str(item.get("source_time", "")),
                    message_id=str(item["message_id"]),
                    attempts=int(item.get("attempts", 0)),
                    next_attempt_at_ms=int(item.get("next_attempt_at_ms", 0)),
                )
                for item in raw.get("pending", [])
                if isinstance(item, dict)
            )
            failed = tuple(
                FailedRecording(
                    minute_token=str(item["minute_token"]),
                    message_id=str(item.get("message_id", "")),
                    attempts=int(item.get("attempts", 0)),
                    failed_at_ms=int(item.get("failed_at_ms", 0)),
                    reason=str(item.get("reason", "unknown error")),
                )
                for item in raw.get("failed", [])
                if isinstance(item, dict)
            )
            return MeetingTriggerState(
                cursor_ms=int(raw.get("cursor_ms", 0)),
                processed_tokens=tuple(
                    str(token) for token in raw.get("processed_tokens", [])
                ),
                skipped_tokens=tuple(
                    str(token) for token in raw.get("skipped_tokens", [])
                ),
                failed=failed,
                pending=pending,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError("meeting trigger state is invalid") from exc

    def save(self, state: MeetingTriggerState) -> None:
        root = self._read_root()
        users = root.setdefault("users", {})
        if not isinstance(users, dict):
            raise CheckpointError("meeting trigger state has invalid users")
        users[self.user_open_id] = {
            "cursor_ms": state.cursor_ms,
            "processed_tokens": list(state.processed_tokens),
            "skipped_tokens": list(state.skipped_tokens),
            "failed": [asdict(item) for item in state.failed],
            "pending": [asdict(item) for item in state.pending],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.part")
        try:
            with temporary.open("wb") as output:
                output.write(
                    (json.dumps(root, ensure_ascii=False, indent=2) + "\n").encode()
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
                f"unable to save meeting trigger state: {exc}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _read_root(self) -> dict[str, object]:
        if not self.path.exists():
            return {"users": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"unable to read meeting trigger state: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CheckpointError("meeting trigger state must be a JSON object")
        return payload


class MeetingTrigger:
    """Turns new 智能纪要助手 recording messages into the existing pipeline."""

    def __init__(
        self,
        user: User,
        source: UserIdentityClient,
        pipeline: MeetingSummary,
        state_store: MeetingTriggerStateStore,
        initial_lookback_ms: int = 600_000,
        retry_base_ms: int = 60_000,
        retry_max_ms: int = 1_800_000,
        max_attempts: int = 5,
    ) -> None:
        self.user = user
        self.source = source
        self.pipeline = pipeline
        self.state_store = state_store
        self.initial_lookback_ms = initial_lookback_ms
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts

    def run(self, end_time_ms: int | None = None) -> MeetingTriggerResult:
        now_ms = end_time_ms if end_time_ms is not None else time.time_ns() // 1_000_000
        state = self.state_store.load()
        if state is None:
            state = MeetingTriggerState(
                cursor_ms=max(now_ms - self.initial_lookback_ms, 0),
                processed_tokens=(),
                skipped_tokens=(),
                failed=(),
                pending=(),
            )
        triggers = self.source.list_minutes_triggers(state.cursor_ms, now_ms)
        state = self._enqueue(state, triggers, now_ms)
        self.state_store.save(state)

        processed = 0
        sent = 0
        suppressed = 0
        skipped = 0
        deferred = 0
        failed = 0
        for item in tuple(state.pending):
            if item.next_attempt_at_ms > now_ms:
                continue
            recording = Recording(
                meeting_id=f"minute-{item.minute_token}",
                duration="",
                url=item.url,
                minute_token=item.minute_token,
                source_name=item.source_name,
                source_time=item.source_time,
            )
            try:
                result = self.pipeline.run_recording(recording)
            except NoEffectiveSpeechError as exc:
                skipped += 1
                logging.getLogger(__name__).info(
                    "meeting user=%s minute=%s stage=asr status=skipped reason=%s",
                    self.user.open_id,
                    item.minute_token[-6:],
                    exc,
                )
                state = self._skip(state, item.minute_token)
            except (ConfigurationError, ValueError) as exc:
                failed += 1
                logging.getLogger(__name__).error(
                    "meeting user=%s minute=%s stage=task status=permanent_failure "
                    "attempt=%s reason=%s",
                    self.user.open_id,
                    item.minute_token[-6:],
                    item.attempts + 1,
                    exc,
                )
                state = self._fail(state, item, now_ms, str(exc))
            except AssistantError as exc:
                attempts = item.attempts + 1
                if attempts >= self.max_attempts:
                    failed += 1
                    logging.getLogger(__name__).error(
                        "meeting user=%s minute=%s stage=task status=retry_exhausted "
                        "attempt=%s reason=%s",
                        self.user.open_id,
                        item.minute_token[-6:],
                        attempts,
                        exc,
                    )
                    state = self._fail(state, item, now_ms, str(exc))
                else:
                    deferred += 1
                    logging.getLogger(__name__).warning(
                        "meeting user=%s minute=%s stage=task status=retry_scheduled "
                        "attempt=%s/%s reason=%s",
                        self.user.open_id,
                        item.minute_token[-6:],
                        attempts,
                        self.max_attempts,
                        exc,
                    )
                    state = self._defer(state, item, now_ms)
            else:
                processed += 1
                if result.message_id:
                    sent += 1
                else:
                    suppressed += 1
                    logging.getLogger(__name__).info(
                        "meeting card suppressed for minute token ending in %s: "
                        "no business content",
                        item.minute_token[-4:],
                    )
                state = self._complete(state, item.minute_token)
            self.state_store.save(state)
        return MeetingTriggerResult(
            user_open_id=self.user.open_id,
            discovered=len(triggers),
            processed=processed,
            sent=sent,
            suppressed=suppressed,
            skipped=skipped,
            deferred=deferred,
            failed=failed,
            pending=len(state.pending),
        )

    def _enqueue(
        self,
        state: MeetingTriggerState,
        triggers: list[MinutesTriggerMessage],
        cursor_ms: int,
    ) -> MeetingTriggerState:
        completed = (
            set(state.processed_tokens)
            | set(state.skipped_tokens)
            | {item.minute_token for item in state.failed}
        )
        pending = {item.minute_token: item for item in state.pending}
        for trigger in triggers:
            if trigger.minute_token in completed or trigger.minute_token in pending:
                continue
            logging.getLogger(__name__).info(
                "meeting user=%s minute=%s stage=discover status=enqueued message=%s",
                self.user.open_id,
                trigger.minute_token[-6:],
                trigger.message_id,
            )
            pending[trigger.minute_token] = PendingRecording(
                minute_token=trigger.minute_token,
                url=trigger.url,
                source_name=trigger.source_name,
                source_time=trigger.source_time,
                message_id=trigger.message_id,
            )
        return MeetingTriggerState(
            cursor_ms=max(state.cursor_ms, cursor_ms),
            processed_tokens=state.processed_tokens,
            skipped_tokens=state.skipped_tokens,
            failed=state.failed,
            pending=tuple(pending.values()),
        )

    def _defer(
        self,
        state: MeetingTriggerState,
        item: PendingRecording,
        now_ms: int,
    ) -> MeetingTriggerState:
        attempts = item.attempts + 1
        delay = min(
            self.retry_base_ms * (2 ** min(attempts - 1, 10)), self.retry_max_ms
        )
        replacement = PendingRecording(
            minute_token=item.minute_token,
            url=item.url,
            source_name=item.source_name,
            source_time=item.source_time,
            message_id=item.message_id,
            attempts=attempts,
            next_attempt_at_ms=now_ms + delay,
        )
        return MeetingTriggerState(
            cursor_ms=state.cursor_ms,
            processed_tokens=state.processed_tokens,
            skipped_tokens=state.skipped_tokens,
            failed=state.failed,
            pending=tuple(
                replacement if pending.minute_token == item.minute_token else pending
                for pending in state.pending
            ),
        )

    @staticmethod
    def _skip(state: MeetingTriggerState, minute_token: str) -> MeetingTriggerState:
        skipped = tuple(dict.fromkeys((*state.skipped_tokens, minute_token)))
        return MeetingTriggerState(
            cursor_ms=state.cursor_ms,
            processed_tokens=state.processed_tokens,
            skipped_tokens=skipped,
            failed=state.failed,
            pending=tuple(
                item for item in state.pending if item.minute_token != minute_token
            ),
        )

    @staticmethod
    def _fail(
        state: MeetingTriggerState,
        item: PendingRecording,
        now_ms: int,
        reason: str,
    ) -> MeetingTriggerState:
        failure = FailedRecording(
            minute_token=item.minute_token,
            message_id=item.message_id,
            attempts=item.attempts + 1,
            failed_at_ms=now_ms,
            reason=reason[:1000],
        )
        failed = tuple(
            existing
            for existing in state.failed
            if existing.minute_token != item.minute_token
        ) + (failure,)
        return MeetingTriggerState(
            cursor_ms=state.cursor_ms,
            processed_tokens=state.processed_tokens,
            skipped_tokens=state.skipped_tokens,
            failed=failed,
            pending=tuple(
                pending
                for pending in state.pending
                if pending.minute_token != item.minute_token
            ),
        )

    @staticmethod
    def _complete(state: MeetingTriggerState, minute_token: str) -> MeetingTriggerState:
        processed = tuple(dict.fromkeys((*state.processed_tokens, minute_token)))
        return MeetingTriggerState(
            cursor_ms=state.cursor_ms,
            processed_tokens=processed,
            skipped_tokens=state.skipped_tokens,
            failed=state.failed,
            pending=tuple(
                item for item in state.pending if item.minute_token != minute_token
            ),
        )
