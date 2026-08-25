from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from feishu_assistant.domain import Conversation, ConversationType, User
from feishu_assistant.errors import CheckpointError, ConfigurationError, FeishuAPIError
from feishu_assistant.feishu_client import FeishuMessage, Recording
from feishu_assistant.meeting_summary import (
    MeetingMinutes,
    MeetingSummary,
    MeetingSummaryResult,
)
from feishu_assistant.message_digest import (
    DigestAttention,
    DigestBatch,
    DigestEvent,
    DigestTodo,
    MessageCheckpointStore,
    MessageDigest,
    MessageDigestSummary,
)
from feishu_assistant.runtime_store import RuntimeStore
from feishu_assistant.workspace import (
    ARCHIVE_KEY_FIELD,
    FIELD_DATETIME,
    MEETING_MINUTES_TABLE,
    MESSAGE_DIGEST_TABLE,
    BitableArchiver,
    BitableClient,
    MeetingMinutesFields,
    MessageDigestFields,
    UserWorkspace,
    WorkspaceField,
    WorkspaceManager,
    WorkspaceStore,
    meeting_minutes_record,
    message_digest_record,
)

TIMEZONE = ZoneInfo("Asia/Shanghai")


def _user() -> User:
    return User("ou_user", "刘文涛")


def _summary() -> MessageDigestSummary:
    return MessageDigestSummary(
        key_events=(DigestEvent("支付接口要求今天完成", "研发群", "薛量"),),
        todos=(DigestTodo("完成支付接口", "刘文涛", "今天", "研发群", "薛量"),),
        other_attention=(
            DigestAttention("下周可能调整测试安排", "研发群", "薛量"),
        ),
    )


def _minutes() -> MeetingMinutes:
    return MeetingMinutes(
        topic="迭代评审",
        conclusions=("本周五发布",),
        discussions=("性能压测结果可接受",),
        todos=(DigestTodo("补充回归用例", "薛量", "周四"),),
        pending_confirmations=("是否需要灰度",),
    )


def _recording() -> Recording:
    return Recording(
        meeting_id="minute-obcnh5q78bidb2x58js13x14",
        duration="600",
        url="https://vc.feishu.cn/minutes/obcnh5q78bidb2x58js13x14",
        minute_token="obcnh5q78bidb2x58js13x14",
        source_name="迭代评审",
        source_time="2026-08-19 10:00",
    )


def test_record_mappers_only_use_central_schema_fields() -> None:
    digest_record = message_digest_record(
        _summary(), 1_786_665_600, 1_786_680_000, 12, ("研发群", "薛量"), TIMEZONE
    )
    meeting_record = meeting_minutes_record(_minutes(), _recording())

    digest_names = {field.name for field in MESSAGE_DIGEST_TABLE.fields}
    meeting_names = {field.name for field in MEETING_MINUTES_TABLE.fields}
    assert set(digest_record) <= digest_names - {ARCHIVE_KEY_FIELD}
    assert set(meeting_record) <= meeting_names - {ARCHIVE_KEY_FIELD}

    assert digest_record[MessageDigestFields.MESSAGE_COUNT] == 12
    assert digest_record[MessageDigestFields.DATE] == 1_786_636_800_000
    assert digest_record[MessageDigestFields.SOURCES] == "研发群、薛量"
    assert digest_record[MessageDigestFields.KEY_EVENTS] == (
        "1. 支付接口要求今天完成｜研发群 · 薛量"
    )
    assert digest_record[MessageDigestFields.TODOS] == (
        "完成支付接口｜刘文涛｜今天｜研发群 · 薛量"
    )
    assert digest_record[MessageDigestFields.OTHER_ATTENTION] == (
        "1. 下周可能调整测试安排｜研发群 · 薛量"
    )
    assert meeting_record[MeetingMinutesFields.TOPIC] == "迭代评审"
    assert meeting_record[MeetingMinutesFields.DATE] == 1_787_068_800_000
    assert meeting_record[MeetingMinutesFields.TODOS] == "补充回归用例｜薛量｜周四"


def test_workspace_store_round_trip_and_errors(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspace-state.json")
    assert store.load("ou_user") is None

    workspace = UserWorkspace(
        open_id="ou_user",
        app_token="bascn_1",
        app_name="刘文涛 的工作记录",
        app_url="https://vc.feishu.cn/base/bascn_1",
        message_table_id="tbl_msg",
        meeting_table_id="tbl_meeting",
        bootstrap_table_id="tbl_bootstrap",
    )
    store.save(workspace)
    assert store.load("ou_user") == workspace

    (tmp_path / "workspace-state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckpointError):
        store.load("ou_user")


class FakeBitableClient:
    """In-memory stand-in for BitableClient used by the manager and archiver."""

    def __init__(self, app_exists: bool = True) -> None:
        self.app_exists_result = app_exists
        self.identity_checks = 0
        self.table_list_calls = 0
        self.field_list_calls = 0
        self.archive_list_calls = 0
        self.apps_created: list[str] = []
        self.tables_created: list[str] = []
        self.deleted_tables: list[str] = []
        self.fields_created: list[tuple[str, str]] = []
        self.records_created: list[dict[str, object]] = []
        self.tables: dict[str, tuple[str, list[str]]] = {}
        self.records: dict[str, str] = {}
        self.current_app_token = ""
        self.current_app_name = ""
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter}"

    def ensure_identity(self) -> None:
        self.identity_checks += 1

    def create_app(self, name: str) -> tuple[str, str, str]:
        app_token = self._next_id("bascn")
        self.apps_created.append(name)
        self.current_app_token = app_token
        self.current_app_name = name
        # Feishu auto-creates one default table with every new app.
        default_id = self._next_id("tbl")
        self.tables[default_id] = ("数据表", [])
        return app_token, f"https://vc.feishu.cn/base/{app_token}", default_id

    def app_exists(self, app_token: str) -> bool:
        return self.app_exists_result

    def list_tables(self, app_token: str) -> list[dict[str, object]]:
        self.table_list_calls += 1
        return [
            {"table_id": table_id, "name": state[0]}
            for table_id, state in self.tables.items()
        ]

    def create_table(self, app_token: str, spec: Any) -> str:
        table_id = self._next_id("tbl")
        self.tables[table_id] = (spec.name, [field.name for field in spec.fields])
        self.tables_created.append(spec.name)
        return table_id

    def delete_table(self, app_token: str, table_id: str) -> None:
        state = self.tables.pop(table_id, None)
        self.deleted_tables.append(state[0] if state else table_id)

    def list_fields(self, app_token: str, table_id: str) -> list[dict[str, object]]:
        self.field_list_calls += 1
        state = self.tables.get(table_id)
        return [{"field_name": name} for name in state[1]] if state else []

    def create_field(
        self, app_token: str, table_id: str, field: WorkspaceField
    ) -> None:
        state = self.tables[table_id]
        self.tables[table_id] = (state[0], [*state[1], field.name])
        self.fields_created.append((table_id, field.name))

    def create_record(
        self, app_token: str, table_id: str, fields: dict[str, object]
    ) -> str:
        record_id = self._next_id("rec")
        self.records[record_id] = str(fields.get(ARCHIVE_KEY_FIELD, ""))
        self.records_created.append(fields)
        return record_id

    def find_record_by_archive_key(
        self, app_token: str, table_id: str, archive_key: str
    ) -> str | None:
        for record_id, key in self.records.items():
            if key == archive_key:
                return record_id
        return None

    def list_archive_records(
        self, app_token: str, table_id: str
    ) -> dict[str, str]:
        self.archive_list_calls += 1
        return {key: record_id for record_id, key in self.records.items() if key}


def _manager(tmp_path: Path, client: FakeBitableClient) -> WorkspaceManager:
    return WorkspaceManager(
        client,
        WorkspaceStore(tmp_path / "workspace-state.json"),  # type: ignore[arg-type]
    )


def test_manager_can_provision_message_only_without_deleting_meeting_data(
    tmp_path: Path,
) -> None:
    client = FakeBitableClient()
    manager = WorkspaceManager(
        client,
        WorkspaceStore(tmp_path / "workspace-state.json"),  # type: ignore[arg-type]
        include_meeting=False,
    )

    workspace = manager.ensure_workspace(_user())

    assert workspace.message_table_id
    assert workspace.meeting_table_id == ""
    assert client.tables_created == ["消息摘要"]
    assert client.deleted_tables == ["数据表"]
    assert {state[0] for state in client.tables.values()} == {"消息摘要"}


def test_manager_creates_workspace_once_and_cleans_default_table(
    tmp_path: Path,
) -> None:
    client = FakeBitableClient()
    manager = _manager(tmp_path, client)

    first = manager.ensure_workspace(_user())
    second = manager.ensure_workspace(_user())

    assert first == second
    assert first.app_name == "刘文涛 的工作记录"
    assert first.message_table_id and first.meeting_table_id
    assert client.apps_created == ["刘文涛 的工作记录"]
    assert client.tables_created == ["消息摘要", "会议纪要"]
    assert client.deleted_tables == ["数据表"]
    table_names = {state[0] for state in client.tables.values()}
    assert table_names == {"消息摘要", "会议纪要"}
    assert first.bootstrap_table_id == ""
    stored = manager.store.load("ou_user")
    assert stored == first


def test_manager_only_deletes_the_bootstrap_table_it_created(tmp_path: Path) -> None:
    client = FakeBitableClient()
    client.tables["tbl_user"] = ("用户自建表", [])

    workspace = _manager(tmp_path, client).ensure_workspace(_user())

    assert workspace.bootstrap_table_id == ""
    assert client.deleted_tables == ["数据表"]
    assert client.tables["tbl_user"][0] == "用户自建表"


def test_manager_creates_new_app_when_local_metadata_is_lost(tmp_path: Path) -> None:
    client = FakeBitableClient()
    manager = _manager(tmp_path, client)
    original = manager.ensure_workspace(_user())
    (tmp_path / "workspace-state.json").unlink()

    recreated = manager.ensure_workspace(_user())

    assert recreated.app_token != original.app_token
    assert client.apps_created == ["刘文涛 的工作记录", "刘文涛 的工作记录"]


def test_manager_persists_app_metadata_immediately_after_app_creation(
    tmp_path: Path,
) -> None:
    class FailFirstTableClient(FakeBitableClient):
        def __init__(self) -> None:
            super().__init__()
            self.table_calls = 0

        def create_table(self, app_token: str, spec: Any) -> str:
            self.table_calls += 1
            if self.table_calls == 1:
                raise FeishuAPIError("uncertain table create")
            return super().create_table(app_token, spec)

    client = FailFirstTableClient()
    manager = _manager(tmp_path, client)

    with pytest.raises(FeishuAPIError, match="uncertain"):
        manager.ensure_workspace(_user())

    stored = manager.store.load("ou_user")
    assert stored is not None and stored.app_token
    assert stored.message_table_id == ""
    assert stored.meeting_table_id == ""
    assert client.apps_created == ["刘文涛 的工作记录"]

    completed = manager.ensure_workspace(_user())

    assert completed.app_token == stored.app_token
    assert completed.message_table_id and completed.meeting_table_id
    assert client.apps_created == ["刘文涛 的工作记录"]


def test_manager_recreates_deleted_app(tmp_path: Path) -> None:
    client = FakeBitableClient()
    manager = _manager(tmp_path, client)
    manager.ensure_workspace(_user())
    client.app_exists_result = False

    recreated = manager.ensure_workspace(_user())

    assert client.apps_created == ["刘文涛 的工作记录", "刘文涛 的工作记录"]
    assert recreated.app_token != ""


def test_manager_creates_missing_fields(tmp_path: Path) -> None:
    client = FakeBitableClient()
    manager = _manager(tmp_path, client)
    workspace = manager.ensure_workspace(_user())
    target = client.tables[workspace.message_table_id]
    client.tables[workspace.message_table_id] = (target[0], target[1][:-1])

    manager.ensure_workspace(_user())

    assert client.fields_created == [(workspace.message_table_id, ARCHIVE_KEY_FIELD)]


def test_workspace_schema_version_skips_remote_validation_after_restart(
    tmp_path: Path,
) -> None:
    client = FakeBitableClient()
    workspace_store = WorkspaceStore(tmp_path / "workspace-state.json")
    runtime_path = tmp_path / "oauth.sqlite3"
    first = WorkspaceManager(client, workspace_store, RuntimeStore(runtime_path))
    workspace = first.ensure_workspace(_user())
    calls = (
        client.identity_checks,
        client.table_list_calls,
        client.field_list_calls,
    )

    second = WorkspaceManager(client, workspace_store, RuntimeStore(runtime_path))
    assert second.ensure_workspace(_user()) == workspace
    assert (
        client.identity_checks,
        client.table_list_calls,
        client.field_list_calls,
    ) == calls


def test_manager_rejects_existing_field_with_wrong_type(tmp_path: Path) -> None:
    class WrongFieldTypeClient(FakeBitableClient):
        def list_fields(self, app_token: str, table_id: str) -> list[dict[str, object]]:
            fields = super().list_fields(app_token, table_id)
            for field in fields:
                field["type"] = 1
            return fields

    client = WrongFieldTypeClient()

    with pytest.raises(ConfigurationError, match="日期"):
        _manager(tmp_path, client).ensure_workspace(_user())


def test_date_fields_are_native_datetime_fields() -> None:
    assert MESSAGE_DIGEST_TABLE.fields[0].name == MessageDigestFields.DATE
    assert MEETING_MINUTES_TABLE.fields[0].name == MeetingMinutesFields.DATE
    assert MESSAGE_DIGEST_TABLE.fields[0].field_type == FIELD_DATETIME
    assert MESSAGE_DIGEST_TABLE.fields[0].property == {"date_formatter": "yyyy-MM-dd"}


def test_archiver_deduplicates_and_creates_records(tmp_path: Path) -> None:
    client = FakeBitableClient()
    archiver = BitableArchiver(
        WorkspaceStore(tmp_path / "workspace-state.json"),
        lambda user: client,  # type: ignore[arg-type]
        TIMEZONE,
    )

    first = archiver.archive_message_digest(
        _user(),
        "dg_key_1",
        _summary(),
        start_time=1_786_665_600,
        end_time=1_786_680_000,
        message_count=12,
        source_names=("研发群",),
    )
    second = archiver.archive_message_digest(
        _user(),
        "dg_key_1",
        _summary(),
        start_time=1_786_665_600,
        end_time=1_786_680_000,
        message_count=12,
        source_names=("研发群",),
    )
    meeting_id = archiver.archive_meeting_minutes(
        _user(), "mt_key_1", _minutes(), recording=_recording()
    )

    assert first == second
    assert len(client.records_created) == 2
    assert client.table_list_calls == 2
    assert client.field_list_calls == 2
    assert client.records_created[0][ARCHIVE_KEY_FIELD] == "dg_key_1"
    assert client.records_created[1][ARCHIVE_KEY_FIELD] == "mt_key_1"
    assert meeting_id != first


def test_durable_archive_index_skips_remote_search_after_restart(
    tmp_path: Path,
) -> None:
    client = FakeBitableClient()
    workspace_store = WorkspaceStore(tmp_path / "workspace-state.json")
    runtime_path = tmp_path / "oauth.sqlite3"

    def archiver() -> BitableArchiver:
        return BitableArchiver(
            workspace_store,
            lambda user: client,  # type: ignore[arg-type]
            TIMEZONE,
            runtime_store=RuntimeStore(runtime_path),
        )

    first = archiver().archive_message_digest(
        _user(),
        "durable_key",
        _summary(),
        start_time=1_786_665_600,
        end_time=1_786_680_000,
        message_count=1,
        source_names=("研发群",),
    )
    second = archiver().archive_message_digest(
        _user(),
        "durable_key",
        _summary(),
        start_time=1_786_665_600,
        end_time=1_786_680_000,
        message_count=1,
        source_names=("研发群",),
    )

    assert first == second
    assert client.archive_list_calls == 1
    assert len(client.records_created) == 1


def test_uncertain_archive_searches_remote_before_retrying_create(
    tmp_path: Path,
) -> None:
    class UncertainClient(FakeBitableClient):
        failed = False

        def create_record(
            self, app_token: str, table_id: str, fields: dict[str, object]
        ) -> str:
            record_id = super().create_record(app_token, table_id, fields)
            if not self.failed:
                self.failed = True
                raise FeishuAPIError("connection lost after remote commit")
            return record_id

    client = UncertainClient()
    workspace_store = WorkspaceStore(tmp_path / "workspace-state.json")
    runtime_path = tmp_path / "oauth.sqlite3"

    def archiver() -> BitableArchiver:
        return BitableArchiver(
            workspace_store,
            lambda user: client,  # type: ignore[arg-type]
            TIMEZONE,
            runtime_store=RuntimeStore(runtime_path),
        )

    with pytest.raises(FeishuAPIError, match="connection lost"):
        archiver().archive_message_digest(
            _user(),
            "uncertain_key",
            _summary(),
            start_time=1,
            end_time=2,
            message_count=1,
            source_names=("研发群",),
        )

    recovered = archiver().archive_message_digest(
        _user(),
        "uncertain_key",
        _summary(),
        start_time=1,
        end_time=2,
        message_count=1,
        source_names=("研发群",),
    )

    assert recovered
    assert len(client.records_created) == 1


def test_archiver_creates_an_independent_workspace_per_user(tmp_path: Path) -> None:
    clients = {
        "ou_first": FakeBitableClient(),
        "ou_second": FakeBitableClient(),
    }
    archiver = BitableArchiver(
        WorkspaceStore(tmp_path / "workspace-state.json"),
        lambda user: clients[user.open_id],  # type: ignore[arg-type]
        TIMEZONE,
    )

    archiver.archive_message_digest(
        User("ou_first", "用户一"),
        "dg_first",
        _summary(),
        start_time=1_786_665_600,
        end_time=1_786_680_000,
        message_count=1,
        source_names=("群一",),
    )
    archiver.archive_message_digest(
        User("ou_second", "用户二"),
        "dg_second",
        _summary(),
        start_time=1_786_665_600,
        end_time=1_786_680_000,
        message_count=1,
        source_names=("群二",),
    )

    store = archiver.store
    first = store.load("ou_first")
    second = store.load("ou_second")
    assert first is not None and second is not None
    assert first.open_id == "ou_first"
    assert second.open_id == "ou_second"
    assert clients["ou_first"].apps_created == ["用户一 的工作记录"]
    assert clients["ou_second"].apps_created == ["用户二 的工作记录"]


def test_bitable_client_builds_user_identity_commands() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> dict[str, object]:
        calls.append(command)
        if "whoami" in command:
            return {"onBehalfOf": {"openId": "ou_user", "userName": "刘文涛"}}
        joined = " ".join(command)
        if "/base/v3/bases" in joined:
            return {
                "code": 0,
                "data": {
                    "data": [["x", "dg_key"]],
                    "record_id_list": ["rec_1"],
                    "fields": ["摘要主题", ARCHIVE_KEY_FIELD],
                },
            }
        if "POST" in command and "/apps" in joined:
            if "/records" in joined:
                return {
                    "code": 0,
                    "data": {"record": {"record_id": "rec_new"}},
                }
            return {
                "code": 0,
                "data": {"app": {"app_token": "bascn_9", "url": "https://x/base"}},
            }
        if "POST" in command and "/fields" in joined:
            return {"code": 0, "data": {}}
        raise AssertionError(f"unexpected command: {command}")

    client = BitableClient("ou_user", profile="user-one", runner=runner)
    client.ensure_identity()
    app_token, url, bootstrap_table_id = client.create_app("刘文涛 的工作记录")
    record_id = client.create_record(
        "bascn_9", "tbl_1", {"摘要主题": "x", ARCHIVE_KEY_FIELD: "dg_key"}
    )
    client.create_field("bascn_9", "tbl_1", WorkspaceField("新增字段", 1))
    found = client.find_record_by_archive_key("bascn_9", "tbl_1", "dg_key")

    assert app_token == "bascn_9"
    assert url == "https://x/base"
    assert bootstrap_table_id == ""
    assert record_id == "rec_new"
    assert found == "rec_1"
    assert all(command[1:3] == ["--profile", "user-one"] for command in calls)
    assert all("--as" in command and "user" in command for command in calls[1:])
    list_call = next(
        command for command in calls if "/base/v3/bases" in " ".join(command)
    )
    list_params = json.loads(list_call[list_call.index("--params") + 1])
    conditions = json.loads(list_params["filter"])["conditions"]
    assert conditions == [[ARCHIVE_KEY_FIELD, "==", "dg_key"]]


def test_bitable_client_identity_mismatch_and_not_found() -> None:
    def wrong_user(_: list[str]) -> dict[str, object]:
        return {"onBehalfOf": {"openId": "ou_other"}}

    client = BitableClient("ou_user", runner=wrong_user)
    with pytest.raises(FeishuAPIError):
        client.ensure_identity()

    def not_found(args: list[str]) -> dict[str, object]:
        if "whoami" in args:
            return {"onBehalfOf": {"openId": "ou_user"}}
        return {
            "ok": False,
            "error": {
                "type": "api",
                "subtype": "not_found",
                "code": 404,
                "message": "HTTP 404: app deleted",
            },
        }

    missing = BitableClient("ou_user", runner=not_found)
    assert missing.app_exists("bascn_gone") is False


def test_bitable_client_configures_date_group_sort_and_hidden_archive_key() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> dict[str, object]:
        calls.append(command)
        if command[2:4] == ["GET", "/open-apis/base/v3/bases/base/tables/tbl/views"]:
            return {
                "code": 0,
                "data": {"items": [{"view_id": "viw_1", "type": "grid"}]},
            }
        return {"code": 0, "data": {}}

    client = BitableClient("ou_user", runner=runner)
    client.configure_grid_view(
        "base",
        "tbl",
        MESSAGE_DIGEST_TABLE,
        MessageDigestFields.TIME_RANGE,
    )

    bodies = [
        json.loads(call[call.index("--data") + 1]) for call in calls if "--data" in call
    ]
    assert bodies[0] == {"group_config": [{"field": "日期", "desc": False}]}
    assert bodies[1]["sort_config"] == [
        {"field": "日期", "desc": False},
        {"field": "时间范围", "desc": False},
    ]
    assert ARCHIVE_KEY_FIELD not in bodies[2]["visible_fields"]


class FakeArchiver:
    def __init__(self) -> None:
        self.digest_calls: list[tuple[str, MessageDigestSummary, tuple[str, ...]]] = []
        self.meeting_calls: list[tuple[str, MeetingMinutes, str]] = []

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
    ) -> str:
        self.digest_calls.append((archive_key, summary, source_names))
        return f"rec_digest_{len(self.digest_calls)}"

    def archive_meeting_minutes(
        self,
        user: User,
        archive_key: str,
        minutes: MeetingMinutes,
        *,
        recording: Recording,
        message_id: str = "",
    ) -> str:
        self.meeting_calls.append((archive_key, minutes, message_id))
        return f"rec_meeting_{len(self.meeting_calls)}"


class DigestFeishu:
    def __init__(self, messages: list[FeishuMessage]):
        self.messages = messages
        self.sent: list[str] = []

    def list_messages(
        self, chat_id: str, start_time: int, end_time: int
    ) -> list[FeishuMessage]:
        return self.messages

    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str:
        self.sent.append(idempotency_key)
        return "om_digest"


class DigestLLM:
    def summarize_messages(self, messages: list[FeishuMessage]) -> MessageDigestSummary:
        return _summary()


def _digest(
    store: MessageCheckpointStore,
    archiver: FakeArchiver | None,
    feishu: DigestFeishu,
) -> MessageDigest:
    return MessageDigest(
        reader=feishu,
        sender=feishu,
        llm=DigestLLM(),
        user=_user(),
        conversations=[
            Conversation("oc_chat", "研发群", ConversationType.GROUP, enabled=True)
        ],
        checkpoint_store=store,
        initial_lookback_seconds=86_400,
        timezone=TIMEZONE,
        archiver=archiver,
    )


def _message() -> FeishuMessage:
    return FeishuMessage(
        message_id="om_new",
        chat_id="oc_chat",
        msg_type="text",
        create_time=1_786_665_600,
        sender_id="ou_sender",
        sender_type="user",
        text="请在今天完成支付接口",
        sender_name="薛量",
    )


def test_digest_archives_after_push_and_replays_on_recovery(
    tmp_path: Path,
) -> None:
    store = MessageCheckpointStore(tmp_path / "checkpoint.json")
    store.save(1_786_665_000)
    feishu = DigestFeishu([_message()])
    archiver = FakeArchiver()
    outcome = _digest(store, archiver, feishu).run(end_time=1_786_680_000)

    assert outcome.sent and outcome.archive_record_id == "rec_digest_1"
    key, summary, sources = archiver.digest_calls[0]
    assert key == feishu.sent[0]
    assert summary == _summary()
    assert sources == ("研发群",)
    assert store.load() == 1_786_680_000
    assert store.load_batch() is None

    # Simulate a crash between push and checkpoint completion: the durable
    # batch still exists, so the next run must replay push+archive idempotently.
    replay_archiver = FakeArchiver()
    replay_feishu = DigestFeishu([])
    batch = DigestBatch(
        start_time=1_786_665_001,
        end_time=1_786_680_000,
        idempotency_key=key,
        created_at=1_786_680_001,
        message_count=1,
        card={"schema": "2.0"},
        summary_payload=MessageDigest._summary_payload(_summary()),
        source_names=("研发群",),
    )
    store.begin_batch(batch)
    recovered = _digest(store, replay_archiver, replay_feishu).run(
        end_time=1_786_690_000
    )

    assert recovered.sent and recovered.archive_record_id == "rec_digest_1"
    assert replay_archiver.digest_calls[0][0] == key
    assert replay_feishu.sent == [key]
    assert store.load_batch() is None


def test_digest_recovery_skips_archive_for_legacy_batch(tmp_path: Path) -> None:
    store = MessageCheckpointStore(tmp_path / "checkpoint.json")
    store.begin_batch(
        DigestBatch(101, 110, "dg_legacy", 1_700_000_000, 1, {"schema": "2.0"})
    )
    archiver = FakeArchiver()
    outcome = _digest(store, archiver, DigestFeishu([])).run(end_time=200)

    assert outcome.sent
    assert outcome.archive_record_id == ""
    assert archiver.digest_calls == []
    assert store.load_batch() is None


class MeetingFeishu:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str:
        self.sent.append(idempotency_key)
        return "om_meeting"


class MeetingASR:
    def transcribe(self, recording: Path, source_url: str = "") -> str:
        return "我们决定本周五发布。"


class MeetingLLM:
    def summarize_meeting(self, transcript: str) -> MeetingMinutes:
        return _minutes()


class MeetingRecordingSource:
    def download_recording(self, recording: Recording, output_dir: Path) -> Any:
        output_dir.mkdir(parents=True, exist_ok=True)
        media_path = output_dir / "media.mp4"
        media_path.write_bytes(b"video")
        from feishu_assistant.feishu_client import RecordingFile

        return RecordingFile(
            path=media_path.resolve(),
            content_type="video/mp4",
            size_bytes=5,
            source_url="https://media",
        )


def test_meeting_archives_after_push_with_minute_token_key(
    tmp_path: Path,
) -> None:
    feishu = MeetingFeishu()
    archiver = FakeArchiver()
    pipeline = MeetingSummary(
        feishu=feishu,
        asr=MeetingASR(),
        llm=MeetingLLM(),
        user=_user(),
        artifact_dir=tmp_path,
        recording_source=MeetingRecordingSource(),
        archiver=archiver,
    )

    result = pipeline.run_recording(_recording())

    assert isinstance(result, MeetingSummaryResult)
    assert result.message_id == "om_meeting"
    assert result.archive_record_id == "rec_meeting_1"
    key, minutes, message_id = archiver.meeting_calls[0]
    assert key == feishu.sent[0]
    assert key.endswith("mt_") or key.startswith("mt_")
    assert minutes == _minutes()
    assert message_id == "om_meeting"
