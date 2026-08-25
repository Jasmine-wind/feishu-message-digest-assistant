from __future__ import annotations

from pathlib import Path

import pytest

from feishu_assistant.errors import ASRError, ConfigurationError
from feishu_assistant.reliability import retry_call, single_instance_lock


def test_retry_call_uses_finite_exponential_backoff() -> None:
    calls = 0
    delays: list[float] = []

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ASRError("temporary outage")
        return "ok"

    assert (
        retry_call(
            operation,
            label="asr",
            user_open_id="ou_user",
            attempts=3,
            sleep=delays.append,
        )
        == "ok"
    )
    assert calls == 3
    assert delays == [1.0, 2.0]


def test_retry_call_stops_after_configured_attempts() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise ASRError("provider unavailable")

    with pytest.raises(ASRError, match="unavailable"):
        retry_call(
            operation,
            label="asr",
            user_open_id="ou_user",
            attempts=2,
            sleep=lambda _: None,
        )
    assert calls == 2


def test_retry_call_does_not_retry_permanent_configuration_error() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise ConfigurationError("invalid OAuth profile")

    with pytest.raises(ConfigurationError, match="OAuth"):
        retry_call(
            operation,
            label="oauth",
            user_open_id="ou_user",
            attempts=3,
            sleep=lambda _: None,
        )
    assert calls == 1


def test_single_instance_lock_rejects_overlapping_scheduler(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler.lock"

    with single_instance_lock(lock_path):  # noqa: SIM117
        with pytest.raises(ConfigurationError, match="another scheduler"):
            with single_instance_lock(lock_path):
                pass
