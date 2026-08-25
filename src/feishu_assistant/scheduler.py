from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import CheckpointError, ConfigurationError

_LOGGER = logging.getLogger(__name__)
DIGEST_HOURS = (8, 12, 18)
DEFAULT_DIGEST_TIMES = tuple((hour, 0) for hour in DIGEST_HOURS)


def next_digest_run(
    now: datetime,
    timezone: ZoneInfo,
    hours: tuple[int, ...] = DIGEST_HOURS,
    *,
    times: tuple[tuple[int, int], ...] | None = None,
) -> datetime:
    """Return the next strictly-future configured run time."""
    local_now = now.astimezone(timezone)
    configured_times = tuple(sorted(set(times or tuple((hour, 0) for hour in hours))))
    if not configured_times:
        raise ValueError("at least one digest time is required")
    for hour, minute in configured_times:
        candidate = local_now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if candidate > local_now:
            return candidate
    tomorrow = local_now + timedelta(days=1)
    hour, minute = configured_times[0]
    return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)


class DigestScheduler:
    def __init__(
        self,
        run_digest: Callable[[int], object],
        timezone: ZoneInfo,
        sleep: Callable[[float], None] = time.sleep,
        max_consecutive_failures: int = 3,
        times: tuple[tuple[int, int], ...] = DEFAULT_DIGEST_TIMES,
    ) -> None:
        self.run_digest = run_digest
        self.timezone = timezone
        self.sleep = sleep
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be positive")
        self.max_consecutive_failures = max_consecutive_failures
        self.times = times

    def run_forever(self) -> None:
        consecutive_failures = 0
        while True:
            target = next_digest_run(
                datetime.now(self.timezone),
                self.timezone,
                times=self.times,
            )
            delay = max(0.0, (target - datetime.now(self.timezone)).total_seconds())
            _LOGGER.info("next message digest: %s", target.isoformat())
            self.sleep(delay)
            end_time = int(datetime.now(self.timezone).timestamp())
            try:
                self.run_digest(end_time)
                consecutive_failures = 0
            except (CheckpointError, ConfigurationError):
                _LOGGER.exception(
                    "scheduled message digest stopped by permanent state/config error"
                )
                raise
            except Exception:
                consecutive_failures += 1
                _LOGGER.exception(
                    "scheduled message digest failed after bounded retries "
                    "consecutive_failures=%s/%s",
                    consecutive_failures,
                    self.max_consecutive_failures,
                )
                if consecutive_failures >= self.max_consecutive_failures:
                    raise ConfigurationError(
                        "message digest scheduler stopped after consecutive failures"
                    )
