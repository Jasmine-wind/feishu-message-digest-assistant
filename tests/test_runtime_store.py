from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from feishu_assistant.api_usage import APICallRecorder, endpoint_name, usage_range
from feishu_assistant.runtime_store import RuntimeStore


def test_runtime_store_persists_names_resources_workspace_and_archive(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    store.save_names({"ou_user": "测试用户"})
    store.save_resource("ou_user", "minutes_assistant_chat_id", "oc_minutes")
    store.save_workspace_version("ou_user", "base", "msg", "meeting", 1)
    store.initialize_archive_cache(
        "ou_user", "message", "base", "msg", {"old": "rec_old"}
    )
    store.mark_archive("new", "ou_user", "message", "creating")
    store.mark_archive("new", "ou_user", "message", "succeeded", "rec_new")

    reopened = RuntimeStore(tmp_path / "runtime.sqlite3")
    assert reopened.names({"ou_user", "ou_missing"}) == {"ou_user": "测试用户"}
    assert reopened.resource("ou_user", "minutes_assistant_chat_id") == "oc_minutes"
    assert reopened.workspace_current("ou_user", "base", "msg", "meeting", 1)
    assert reopened.archive_cache_current("ou_user", "message", "base", "msg")
    assert reopened.archive_state("old").record_id == "rec_old"  # type: ignore[union-attr]
    assert reopened.archive_state("new").record_id == "rec_new"  # type: ignore[union-attr]


def test_api_usage_is_local_and_supports_filters(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite3"
    digest = APICallRecorder(path, "message_digest")
    meeting = APICallRecorder(path, "meeting_trigger")
    digest.observe("GET", "/im/v1/messages", "ou_user", True)
    digest.observe("POST", "/im/v1/messages", "ou_user", True)
    meeting.observe("POST", "/authen/v1/refresh_access_token", "ou_user", False)

    start = int(datetime(2020, 1, 1, tzinfo=ZoneInfo("UTC")).timestamp())
    summary = digest.summarize(
        start_timestamp=start,
        end_timestamp=4_102_444_800,
        user_open_id="ou_user",
    )

    assert summary.total == 3
    assert summary.failures == 1
    assert summary.by_endpoint == {
        "auth.refresh": 1,
        "im.message.create": 1,
        "im.messages.list": 1,
    }
    assert summary.by_caller == {"meeting_trigger": 1, "message_digest": 2}
    assert endpoint_name("GET", "/open-apis/im/v1/chats/oc/members") == (
        "im.chat_members.list"
    )


def test_api_usage_write_failure_never_changes_business_result(
    tmp_path, monkeypatch
) -> None:
    recorder = APICallRecorder(tmp_path / "runtime.sqlite3", "archive")

    def unavailable() -> sqlite3.Connection:
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(recorder, "_connect", unavailable)
    recorder.observe("POST", "/bitable/v1/apps/base/tables/table/records", "ou", True)


def test_usage_range_supports_day_and_month() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    day = usage_range(day="2026-08-19", timezone=timezone)
    month = usage_range(month="2026-08", timezone=timezone)

    assert day[1] - day[0] == 86400
    assert datetime.fromtimestamp(month[0], timezone).day == 1
    assert datetime.fromtimestamp(month[1], timezone).month == 9
