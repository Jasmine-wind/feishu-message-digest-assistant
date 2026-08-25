from __future__ import annotations

import pytest

from feishu_assistant import cli


def test_disabled_meeting_commands_exit_before_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    invoked: list[str] = []
    monkeypatch.setattr(cli, "meeting_summary_enabled_from_env", lambda: False)
    monkeypatch.setattr(
        cli, "_run_meeting_triggers", lambda *_: invoked.append("trigger")
    )
    monkeypatch.setattr(
        cli, "_run_meeting_trigger_scheduler", lambda: invoked.append("scheduler")
    )
    monkeypatch.setattr(cli, "_run_pipeline", lambda *_: invoked.append("run"))
    monkeypatch.setattr(
        cli, "_download_recording", lambda *_: invoked.append("download")
    )

    commands = (
        ["meeting-trigger"],
        ["meeting-trigger-scheduler"],
        ["run", "meeting-id"],
        ["download-recording", "meeting-id"],
    )
    for command in commands:
        assert cli.main(command) == 1

    assert invoked == []
    assert capsys.readouterr().err.count("meeting summary is disabled") == 4


def test_disabled_meeting_does_not_block_digest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "meeting_summary_enabled_from_env", lambda: False)
    monkeypatch.setattr(cli, "message_digest_enabled_from_env", lambda: True)
    monkeypatch.setattr(cli, "_run_digest", lambda _: {"sent": False})

    assert cli.main(["digest"]) == 0
    assert '"sent": false' in capsys.readouterr().out
