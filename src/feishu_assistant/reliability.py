from __future__ import annotations

import fcntl
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from .errors import AssistantError, ConfigurationError

T = TypeVar("T")


def retry_call(
    operation: Callable[[], T],
    *,
    label: str,
    user_open_id: str,
    attempts: int,
    base_delay_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry transient AssistantError failures with finite exponential backoff."""
    logger = logging.getLogger(__name__)
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (ConfigurationError, ValueError):
            logger.exception(
                "operation user=%s task=%s status=permanent_failure attempt=%s",
                user_open_id,
                label,
                attempt,
            )
            raise
        except AssistantError as exc:
            if attempt >= attempts:
                logger.error(
                    "operation user=%s task=%s status=retry_exhausted "
                    "attempt=%s/%s reason=%s",
                    user_open_id,
                    label,
                    attempt,
                    attempts,
                    exc,
                )
                raise
            delay = base_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "operation user=%s task=%s status=retry_scheduled "
                "attempt=%s/%s delay_seconds=%s reason=%s",
                user_open_id,
                label,
                attempt,
                attempts,
                delay,
                exc,
            )
            sleep(delay)
    raise AssertionError("retry loop exited unexpectedly")


@contextmanager
def single_instance_lock(path: Path) -> Iterator[None]:
    """Hold a non-blocking advisory lock for one single-host scheduler process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigurationError(
                f"another scheduler instance holds lock: {path}"
            ) from exc
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def blocking_file_lock(path: Path) -> Iterator[None]:
    """Serialize a short cross-process critical section."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
