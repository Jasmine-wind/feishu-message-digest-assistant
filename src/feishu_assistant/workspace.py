from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain import User
from .errors import CheckpointError, ConfigurationError, FeishuAPIError
from .feishu_client import Recording
from .meeting_summary import MeetingMinutes
from .message_digest import MessageDigestSummary, _numbered
from .reliability import single_instance_lock
from .runtime_store import RuntimeStore
from .user_identity_client import JSONRunner, UserIdentityClient

# Bitable field types used by this project. Every business field stays text
# unless the card content is genuinely numeric/date, so the schema remains
# stable and LLM output never drives field creation.
FIELD_TEXT = 1
FIELD_NUMBER = 2
FIELD_DATETIME = 5
DEFAULT_TIMEZONE = ZoneInfo("Asia/Shanghai")

ARCHIVE_KEY_FIELD = "archive_key"


class BitableNotFoundError(FeishuAPIError):
    """The referenced bitable app/table/record no longer exists."""


@dataclass(frozen=True)
class WorkspaceField:
    name: str
    field_type: int
    property: dict[str, object] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceTableSpec:
    key: str
    name: str
    fields: tuple[WorkspaceField, ...]


class MessageDigestFields:
    """Central field names for the 消息摘要 table."""

    DATE = "日期"
    TITLE = "摘要主题"
    TIME_RANGE = "时间范围"
    SOURCES = "消息来源"
    MESSAGE_COUNT = "消息数量"
    KEY_EVENTS = "关键事件"
    TODOS = "待办事项"
    OTHER_ATTENTION = "其他值得关注"


class MeetingMinutesFields:
    """Central field names for the 会议纪要 table."""

    DATE = "日期"
    TOPIC = "会议主题"
    SOURCE_NAME = "会议来源"
    SOURCE_TIME = "会议时间"
    CONCLUSIONS = "核心结论"
    DISCUSSIONS = "重要讨论"
    TODOS = "会议待办"
    PENDING_CONFIRMATIONS = "待确认事项"


MESSAGE_DIGEST_TABLE = WorkspaceTableSpec(
    key="message",
    name="消息摘要",
    fields=(
        WorkspaceField(
            MessageDigestFields.DATE,
            FIELD_DATETIME,
            {"date_formatter": "yyyy-MM-dd"},
        ),
        WorkspaceField(MessageDigestFields.TITLE, FIELD_TEXT),
        WorkspaceField(MessageDigestFields.TIME_RANGE, FIELD_TEXT),
        WorkspaceField(MessageDigestFields.SOURCES, FIELD_TEXT),
        WorkspaceField(MessageDigestFields.MESSAGE_COUNT, FIELD_NUMBER),
        WorkspaceField(MessageDigestFields.KEY_EVENTS, FIELD_TEXT),
        WorkspaceField(MessageDigestFields.TODOS, FIELD_TEXT),
        WorkspaceField(MessageDigestFields.OTHER_ATTENTION, FIELD_TEXT),
        WorkspaceField(ARCHIVE_KEY_FIELD, FIELD_TEXT),
    ),
)

MEETING_MINUTES_TABLE = WorkspaceTableSpec(
    key="meeting",
    name="会议纪要",
    fields=(
        WorkspaceField(
            MeetingMinutesFields.DATE,
            FIELD_DATETIME,
            {"date_formatter": "yyyy-MM-dd"},
        ),
        WorkspaceField(MeetingMinutesFields.TOPIC, FIELD_TEXT),
        WorkspaceField(MeetingMinutesFields.SOURCE_NAME, FIELD_TEXT),
        WorkspaceField(MeetingMinutesFields.SOURCE_TIME, FIELD_TEXT),
        WorkspaceField(MeetingMinutesFields.CONCLUSIONS, FIELD_TEXT),
        WorkspaceField(MeetingMinutesFields.DISCUSSIONS, FIELD_TEXT),
        WorkspaceField(MeetingMinutesFields.TODOS, FIELD_TEXT),
        WorkspaceField(MeetingMinutesFields.PENDING_CONFIRMATIONS, FIELD_TEXT),
        WorkspaceField(ARCHIVE_KEY_FIELD, FIELD_TEXT),
    ),
)


def workspace_app_name(user: User) -> str:
    return f"{user.name} 的工作记录"


def _message_origin(source: str, sender: str) -> str:
    return f"{source.strip() or '未明确'} · {sender.strip() or '未明确'}"


def message_digest_record(
    summary: MessageDigestSummary,
    start_time: int,
    end_time: int,
    message_count: int,
    source_names: tuple[str, ...],
    timezone: ZoneInfo,
) -> dict[str, object]:
    """Map the digest business result onto the centralized table schema."""
    start = datetime.fromtimestamp(start_time, timezone).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(end_time, timezone)
    end_text = end.strftime("%H:%M")
    if end.date() != datetime.fromtimestamp(start_time, timezone).date():
        end_text = end.strftime("%Y-%m-%d %H:%M")
    sources = "、".join(
        dict.fromkeys(name.strip() for name in source_names if name.strip())
    )
    todos = "\n".join(
        f"{todo.task}｜{todo.assignee or '未明确'}｜{todo.deadline or '-'}｜"
        f"{_message_origin(todo.source, todo.sender)}"
        for todo in summary.todos
    )
    return {
        MessageDigestFields.DATE: _date_milliseconds(
            datetime.fromtimestamp(start_time, timezone)
        ),
        MessageDigestFields.TITLE: f"消息摘要 {start} ～ {end_text}",
        MessageDigestFields.TIME_RANGE: f"{start} ～ {end_text}",
        MessageDigestFields.SOURCES: sources or "未明确",
        MessageDigestFields.MESSAGE_COUNT: message_count,
        MessageDigestFields.KEY_EVENTS: _numbered(
            tuple(
                f"{item.content}｜{_message_origin(item.source, item.sender)}"
                for item in summary.key_events
            )
        ),
        MessageDigestFields.TODOS: todos,
        MessageDigestFields.OTHER_ATTENTION: _numbered(
            tuple(
                f"{item.content}｜{_message_origin(item.source, item.sender)}"
                for item in summary.other_attention
            )
        ),
    }


def meeting_minutes_record(
    minutes: MeetingMinutes,
    recording: Recording,
    timezone: ZoneInfo = DEFAULT_TIMEZONE,
) -> dict[str, object]:
    """Map the meeting business result onto the centralized table schema."""
    todos = "\n".join(
        f"{todo.task}｜{todo.assignee or '未明确'}｜{todo.deadline or '-'}"
        for todo in minutes.todos
    )
    record: dict[str, object] = {
        MeetingMinutesFields.TOPIC: minutes.topic.strip() or "未命名会议",
        MeetingMinutesFields.SOURCE_NAME: recording.source_name.strip(),
        MeetingMinutesFields.SOURCE_TIME: recording.source_time.strip(),
        MeetingMinutesFields.CONCLUSIONS: _numbered(minutes.conclusions),
        MeetingMinutesFields.DISCUSSIONS: _numbered(minutes.discussions),
        MeetingMinutesFields.TODOS: todos,
        MeetingMinutesFields.PENDING_CONFIRMATIONS: _numbered(
            minutes.pending_confirmations
        ),
    }
    meeting_date = _parse_source_datetime(recording.source_time, timezone)
    if meeting_date is not None:
        record[MeetingMinutesFields.DATE] = _date_milliseconds(meeting_date)
    return record


def _date_milliseconds(value: datetime) -> int:
    """Return local midnight as the millisecond value required by Base dates."""
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def _parse_source_datetime(value: str, timezone: ZoneInfo) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    normalized = text.replace("年", "-").replace("月", "-").replace("日", " ")
    normalized = " ".join(normalized.split())
    for candidate in (normalized, normalized[:16], normalized[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed.astimezone(timezone)
    return None


@dataclass(frozen=True)
class UserWorkspace:
    open_id: str
    app_token: str
    app_name: str
    app_url: str
    message_table_id: str
    meeting_table_id: str
    bootstrap_table_id: str = ""


class WorkspaceStore:
    """Durable per-user bitable metadata (app_token plus both table ids)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, open_id: str) -> UserWorkspace | None:
        users = self._read_users()
        raw = users.get(open_id)
        if not isinstance(raw, dict):
            return None
        try:
            return UserWorkspace(
                open_id=open_id,
                app_token=str(raw["app_token"]),
                app_name=str(raw.get("app_name", "")),
                app_url=str(raw.get("app_url", "")),
                message_table_id=str(raw.get("message_table_id", "")),
                meeting_table_id=str(raw.get("meeting_table_id", "")),
                bootstrap_table_id=str(raw.get("bootstrap_table_id", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError(f"invalid workspace metadata for {open_id}") from exc

    def save(self, workspace: UserWorkspace) -> None:
        root = self._read_root()
        users = root.setdefault("users", {})
        if not isinstance(users, dict):
            raise CheckpointError("workspace metadata has invalid users")
        users[workspace.open_id] = {
            "app_token": workspace.app_token,
            "app_name": workspace.app_name,
            "app_url": workspace.app_url,
            "message_table_id": workspace.message_table_id,
            "meeting_table_id": workspace.meeting_table_id,
            "bootstrap_table_id": workspace.bootstrap_table_id,
        }
        self._write(root)

    def _read_users(self) -> dict[str, object]:
        users = self._read_root().get("users", {})
        if not isinstance(users, dict):
            raise CheckpointError("workspace metadata has invalid users")
        return users

    def _read_root(self) -> dict[str, object]:
        if not self.path.exists():
            return {"users": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"invalid workspace metadata {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CheckpointError("workspace metadata must be a JSON object")
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
                f"failed to save workspace metadata {self.path}: {exc}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)


_NOT_FOUND_CODES = {404, 1254043, 1254045, 1254601}
WORKSPACE_SCHEMA_VERSION = 1


class BitableClient:
    """Bitable OpenAPI subset executed with each user's own OAuth identity."""

    def __init__(
        self,
        expected_open_id: str,
        profile: str = "",
        runner: JSONRunner | None = None,
    ) -> None:
        if not expected_open_id.strip():
            raise ValueError("expected user open_id must not be empty")
        self.expected_open_id = expected_open_id
        self.profile = profile.strip()
        self._runner = runner or UserIdentityClient._run_json
        self._identity_checked = False

    def ensure_identity(self) -> None:
        """Verify once that the OAuth profile really belongs to this user."""
        if self._identity_checked:
            return
        payload = self._call(["whoami", "--as", "user"])
        identity = payload.get("onBehalfOf")
        open_id = identity.get("openId") if isinstance(identity, dict) else None
        if open_id != self.expected_open_id:
            raise FeishuAPIError(
                "bitable OAuth profile user does not match configured user open_id"
            )
        self._identity_checked = True

    def create_app(self, name: str) -> tuple[str, str, str]:
        data = self._api("POST", "/open-apis/bitable/v1/apps", body={"name": name})
        app = data.get("app")
        if not isinstance(app, dict):
            raise FeishuAPIError("bitable create app returned no app object")
        app_token = app.get("app_token")
        if not isinstance(app_token, str) or not app_token:
            raise FeishuAPIError("bitable create app returned an empty app_token")
        url = app.get("url")
        default_table_id = app.get("default_table_id")
        return (
            app_token,
            url if isinstance(url, str) else "",
            default_table_id if isinstance(default_table_id, str) else "",
        )

    def app_exists(self, app_token: str) -> bool:
        try:
            self._api("GET", f"/open-apis/bitable/v1/apps/{app_token}")
        except BitableNotFoundError:
            return False
        return True

    def list_tables(self, app_token: str) -> list[dict[str, object]]:
        tables: list[dict[str, object]] = []
        page_token = ""
        while True:
            params: dict[str, str] = {"page_size": "100"}
            if page_token:
                params["page_token"] = page_token
            data = self._api(
                "GET", f"/open-apis/bitable/v1/apps/{app_token}/tables", params=params
            )
            items = data.get("items", [])
            if not isinstance(items, list):
                raise FeishuAPIError("bitable table list returned invalid items")
            tables.extend(item for item in items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token
        return tables

    def create_table(self, app_token: str, spec: WorkspaceTableSpec) -> str:
        data = self._api(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables",
            body={
                "table": {
                    "name": spec.name,
                    "default_view_name": "表格",
                    "fields": [
                        {
                            "field_name": field.name,
                            "type": field.field_type,
                            **({"property": field.property} if field.property else {}),
                        }
                        for field in spec.fields
                    ],
                }
            },
        )
        table_id = data.get("table_id")
        if not isinstance(table_id, str) or not table_id:
            raise FeishuAPIError("bitable create table returned an empty table_id")
        return table_id

    def delete_table(self, app_token: str, table_id: str) -> None:
        self._api("DELETE", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}")

    def list_fields(self, app_token: str, table_id: str) -> list[dict[str, object]]:
        fields: list[dict[str, object]] = []
        page_token = ""
        while True:
            params: dict[str, str] = {"page_size": "100"}
            if page_token:
                params["page_token"] = page_token
            data = self._api(
                "GET",
                f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                params=params,
            )
            items = data.get("items", [])
            if not isinstance(items, list):
                raise FeishuAPIError("bitable field list returned invalid items")
            fields.extend(item for item in items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token
        return fields

    def list_views(self, app_token: str, table_id: str) -> list[dict[str, object]]:
        data = self._api(
            "GET",
            f"/open-apis/base/v3/bases/{app_token}/tables/{table_id}/views",
        )
        raw = data.get("items", data.get("views", []))
        if not isinstance(raw, list):
            raise FeishuAPIError("bitable view list returned invalid items")
        return [item for item in raw if isinstance(item, dict)]

    def configure_grid_view(
        self,
        app_token: str,
        table_id: str,
        spec: WorkspaceTableSpec,
        time_field: str,
    ) -> None:
        views = self.list_views(app_token, table_id)
        view = next(
            (
                item
                for item in views
                if item.get("type") in {None, "grid", "GRID"}
                and isinstance(item.get("id", item.get("view_id")), str)
            ),
            None,
        )
        if view is None:
            raise FeishuAPIError("bitable table has no configurable grid view")
        view_id = str(view.get("id", view.get("view_id", "")))
        base_path = (
            f"/open-apis/base/v3/bases/{app_token}/tables/{table_id}/views/{view_id}"
        )
        self._api_any(
            "PUT",
            f"{base_path}/group",
            body={"group_config": [{"field": "日期", "desc": False}]},
        )
        self._api_any(
            "PUT",
            f"{base_path}/sort",
            body={
                "sort_config": [
                    {"field": "日期", "desc": False},
                    {"field": time_field, "desc": False},
                ]
            },
        )
        self._api_any(
            "PUT",
            f"{base_path}/visible_fields",
            body={
                "visible_fields": [
                    field.name
                    for field in spec.fields
                    if field.name != ARCHIVE_KEY_FIELD
                ]
            },
        )

    def create_field(
        self, app_token: str, table_id: str, field: WorkspaceField
    ) -> None:
        body: dict[str, object] = {
            "field_name": field.name,
            "type": field.field_type,
        }
        if field.property:
            body["property"] = field.property
        self._api(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            body=body,
        )

    def create_record(
        self, app_token: str, table_id: str, fields: dict[str, object]
    ) -> str:
        data = self._api(
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            body={"fields": fields},
        )
        record = data.get("record")
        record_id = record.get("record_id") if isinstance(record, dict) else None
        if not isinstance(record_id, str) or not record_id:
            raise FeishuAPIError("bitable create record returned an empty record_id")
        return record_id

    def find_record_by_archive_key(
        self, app_token: str, table_id: str, archive_key: str
    ) -> str | None:
        """Find a previously archived record by its archive_key.

        Uses the v3 Base list API with a text-equality filter so the read only
        needs the base:record:read scope (the v1 records/search endpoint
        additionally requires base:record:retrieve). Rows come back as cell
        arrays aligned with ``fields`` and ``record_id_list``.
        """
        data = self._api(
            "GET",
            f"/open-apis/base/v3/bases/{app_token}/tables/{table_id}/records",
            params={
                "filter": json.dumps(
                    {
                        "logic": "and",
                        "conditions": [[ARCHIVE_KEY_FIELD, "==", archive_key]],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "limit": "10",
            },
        )
        rows = data.get("data", [])
        record_ids = data.get("record_id_list", [])
        field_names = data.get("fields", [])
        if not isinstance(rows, list) or not isinstance(record_ids, list):
            raise FeishuAPIError("bitable record list returned invalid items")
        if not isinstance(field_names, list) or ARCHIVE_KEY_FIELD not in field_names:
            raise FeishuAPIError(
                "bitable record list did not include the archive_key field"
            )
        column = field_names.index(ARCHIVE_KEY_FIELD)
        for position, row in enumerate(rows):
            if (
                isinstance(row, list)
                and position < len(record_ids)
                and position < len(row)
                and row[column] == archive_key
            ):
                record_id = record_ids[position]
                if isinstance(record_id, str):
                    return record_id
        return None

    def list_archive_records(
        self, app_token: str, table_id: str
    ) -> dict[str, str]:
        """Load archive keys once when initializing the durable local index."""
        records: dict[str, str] = {}
        page_token = ""
        while True:
            params = {"limit": "500"}
            if page_token:
                params["page_token"] = page_token
            data = self._api(
                "GET",
                f"/open-apis/base/v3/bases/{app_token}/tables/{table_id}/records",
                params=params,
            )
            raw_rows = data.get("data", [])
            raw_record_ids = data.get("record_id_list", [])
            raw_field_names = data.get("fields", [])
            if not isinstance(raw_rows, list):
                raise FeishuAPIError("bitable record list returned invalid items")
            if not isinstance(raw_record_ids, list):
                raise FeishuAPIError("bitable record list returned invalid items")
            if not isinstance(raw_field_names, list):
                raise FeishuAPIError("bitable record list returned invalid items")
            rows: list[object] = raw_rows
            record_ids: list[object] = raw_record_ids
            field_names: list[object] = raw_field_names
            if ARCHIVE_KEY_FIELD not in field_names:
                raise FeishuAPIError(
                    "bitable record list did not include the archive_key field"
                )
            column = field_names.index(ARCHIVE_KEY_FIELD)
            for position, row in enumerate(rows):
                if not isinstance(row, list) or position >= len(record_ids):
                    continue
                if column >= len(row):
                    continue
                key = row[column]
                record_id = record_ids[position]
                if isinstance(key, str) and key and isinstance(record_id, str):
                    records[key] = record_id
            if not data.get("has_more"):
                break
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token:
                raise FeishuAPIError("bitable record list returned invalid page_token")
            page_token = next_token
        return records

    def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        data = self._api_any(method, path, params=params, body=body)
        if not isinstance(data, dict):
            raise FeishuAPIError(f"bitable API returned invalid data for {path}")
        return data

    def _api_any(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, object] | None = None,
    ) -> object:
        command = ["api", method, path, "--as", "user"]
        if params:
            command.extend(
                [
                    "--params",
                    json.dumps(params, ensure_ascii=False, separators=(",", ":")),
                ]
            )
        if body is not None:
            command.extend(
                ["--data", json.dumps(body, ensure_ascii=False, separators=(",", ":"))]
            )
        command.append("--format")
        command.append("json")
        payload = self._call(command)
        code = payload.get("code", 0)
        if isinstance(code, int) and code != 0:
            self._raise_api_error(code, payload.get("msg", ""), path)
        return payload.get("data", {})

    def _call(self, args: list[str]) -> dict[str, object]:
        command = ["lark-cli"]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend(args)
        payload = self._runner(command)
        if payload.get("ok") is False:
            error = payload.get("error")
            if not isinstance(error, dict):
                raise FeishuAPIError("unknown bitable request error")
            code = error.get("code")
            subtype = str(error.get("subtype", ""))
            message = str(error.get("message", "unknown bitable request error"))
            if subtype == "not_found" or (
                isinstance(code, int) and code in _NOT_FOUND_CODES
            ):
                raise BitableNotFoundError(f"bitable resource not found: {message}")
            raise FeishuAPIError(f"bitable request failed: {message}")
        return payload

    @staticmethod
    def _raise_api_error(code: int, msg: object, path: str) -> None:
        message = str(msg)
        lowered = message.casefold()
        if code in _NOT_FOUND_CODES or "not found" in lowered or "不存在" in message:
            raise BitableNotFoundError(
                f"bitable resource not found for {path}: code={code} msg={message}"
            )
        raise FeishuAPIError(f"bitable API error for {path}: code={code} msg={message}")


class WorkspaceManager:
    """Idempotently provisions one bitable workspace per user."""

    def __init__(
        self,
        client: BitableClient,
        store: WorkspaceStore,
        runtime_store: RuntimeStore | None = None,
        *,
        include_meeting: bool = True,
    ) -> None:
        self.client = client
        self.store = store
        self.runtime_store = runtime_store
        self.include_meeting = include_meeting
        self._ready_workspace: UserWorkspace | None = None

    def invalidate(self) -> None:
        """Force the next archive to validate remote Base metadata again."""
        self._ready_workspace = None

    def ensure_workspace(
        self, user: User, *, validate_remote: bool = True
    ) -> UserWorkspace:
        workspace = self.store.load(user.open_id)
        if (
            not validate_remote
            and workspace is not None
            and workspace == self._ready_workspace
        ):
            return workspace
        if self._workspace_version_current(workspace):
            assert workspace is not None
            self._ready_workspace = workspace
            return workspace
        if self.runtime_store is not None:
            with self.runtime_store.lock("workspace-metadata"):
                workspace = self.store.load(user.open_id)
                if self._workspace_version_current(workspace):
                    assert workspace is not None
                    self._ready_workspace = workspace
                    return workspace
                workspace = self._ensure_workspace_remote(user, workspace)
                self._save_workspace_version(workspace)
                return workspace
        return self._ensure_workspace_remote(user, workspace)

    def _workspace_version_current(
        self, workspace: UserWorkspace | None
    ) -> bool:
        return (
            self.runtime_store is not None
            and workspace is not None
            and bool(workspace.app_token)
            and bool(workspace.message_table_id)
            and (not self.include_meeting or bool(workspace.meeting_table_id))
            and self.runtime_store.workspace_current(
                workspace.open_id,
                workspace.app_token,
                workspace.message_table_id,
                workspace.meeting_table_id,
                WORKSPACE_SCHEMA_VERSION,
            )
        )

    def _save_workspace_version(self, workspace: UserWorkspace) -> None:
        if self.runtime_store is None:
            return
        self.runtime_store.save_workspace_version(
            workspace.open_id,
            workspace.app_token,
            workspace.message_table_id,
            workspace.meeting_table_id,
            WORKSPACE_SCHEMA_VERSION,
        )

    def _ensure_workspace_remote(
        self, user: User, workspace: UserWorkspace | None
    ) -> UserWorkspace:
        logger = logging.getLogger(__name__)
        self.client.ensure_identity()
        app_name = workspace_app_name(user)
        if workspace is not None and workspace.app_token:
            if self.client.app_exists(workspace.app_token):
                workspace = self._ensure_tables(user, workspace)
                workspace = self._cleanup_bootstrap_table(user, workspace)
                self._ready_workspace = workspace
                return workspace
            logger.warning(
                "workspace user=%s app=%s no longer exists, creating a new Base",
                user.open_id,
                workspace.app_token,
            )
            workspace = None
        if workspace is None:
            app_token, app_url, bootstrap_table_id = self.client.create_app(app_name)
            workspace = UserWorkspace(
                open_id=user.open_id,
                app_token=app_token,
                app_name=app_name,
                app_url=app_url,
                message_table_id="",
                meeting_table_id="",
                bootstrap_table_id=bootstrap_table_id,
            )
            self.store.save(workspace)
            logger.info(
                "workspace user=%s stage=create_app app=%s status=succeeded",
                user.open_id,
                app_token,
            )
        workspace = self._ensure_tables(user, workspace)
        workspace = self._cleanup_bootstrap_table(user, workspace)
        self._ready_workspace = workspace
        return workspace

    def _ensure_tables(self, user: User, workspace: UserWorkspace) -> UserWorkspace:
        workspace = self._ensure_table(
            user, workspace, MESSAGE_DIGEST_TABLE, "message_table_id"
        )
        if self.include_meeting:
            workspace = self._ensure_table(
                user, workspace, MEETING_MINUTES_TABLE, "meeting_table_id"
            )
        return workspace

    def _ensure_table(
        self,
        user: User,
        workspace: UserWorkspace,
        spec: WorkspaceTableSpec,
        attribute: str,
    ) -> UserWorkspace:
        logger = logging.getLogger(__name__)
        table_id = str(getattr(workspace, attribute))
        tables = self.client.list_tables(workspace.app_token)
        known_ids = {item.get("table_id") for item in tables}
        if table_id and table_id in known_ids:
            self._ensure_fields(workspace.app_token, table_id, spec)
            self._configure_view(workspace.app_token, table_id, spec)
            return workspace
        by_name = {
            item.get("name"): item.get("table_id")
            for item in tables
            if isinstance(item.get("name"), str)
        }
        reused = by_name.get(spec.name)
        if isinstance(reused, str) and reused:
            table_id = reused
            logger.info(
                "workspace user=%s stage=create_table table=%s status=reused",
                user.open_id,
                spec.name,
            )
        else:
            table_id = self.client.create_table(workspace.app_token, spec)
            logger.info(
                "workspace user=%s stage=create_table table=%s status=created id=%s",
                user.open_id,
                spec.name,
                table_id,
            )
        workspace = replace(workspace, **{attribute: table_id})
        self.store.save(workspace)
        self._ensure_fields(workspace.app_token, table_id, spec)
        self._configure_view(workspace.app_token, table_id, spec)
        return workspace

    def _configure_view(
        self, app_token: str, table_id: str, spec: WorkspaceTableSpec
    ) -> None:
        time_field = (
            MessageDigestFields.TIME_RANGE
            if spec.key == MESSAGE_DIGEST_TABLE.key
            else MeetingMinutesFields.SOURCE_TIME
        )
        configure = getattr(self.client, "configure_grid_view", None)
        if not callable(configure):
            return
        try:
            configure(app_token, table_id, spec, time_field)
        except FeishuAPIError as exc:
            # View APIs require independent scopes and are best-effort. Schema and
            # archival remain available even when an older OAuth grant lacks them.
            logging.getLogger(__name__).warning(
                "workspace stage=configure_view table=%s status=unavailable: %s",
                spec.name,
                exc,
            )

    def _ensure_fields(
        self, app_token: str, table_id: str, spec: WorkspaceTableSpec
    ) -> None:
        existing = {
            str(item.get("field_name")): item.get("type")
            for item in self.client.list_fields(app_token, table_id)
            if isinstance(item.get("field_name"), str)
        }
        for field in spec.fields:
            current_type = existing.get(field.name)
            if field.name not in existing:
                self.client.create_field(app_token, table_id, field)
                logging.getLogger(__name__).info(
                    "workspace stage=create_field table=%s field=%s status=created",
                    spec.name,
                    field.name,
                )
            elif isinstance(current_type, int) and current_type != field.field_type:
                raise ConfigurationError(
                    f"workspace field type mismatch: {spec.name}.{field.name} "
                    f"expected={field.field_type} actual={current_type}"
                )

    def _cleanup_bootstrap_table(
        self, user: User, workspace: UserWorkspace
    ) -> UserWorkspace:
        """Delete only the exact default table returned by app creation."""
        table_id = workspace.bootstrap_table_id
        if not table_id or not workspace.message_table_id:
            return workspace
        managed_table_ids = {workspace.message_table_id}
        if self.include_meeting and workspace.meeting_table_id:
            managed_table_ids.add(workspace.meeting_table_id)
        if table_id not in managed_table_ids:
            try:
                self.client.delete_table(workspace.app_token, table_id)
            except BitableNotFoundError:
                pass
            logging.getLogger(__name__).info(
                "workspace user=%s stage=cleanup table=%s status=deleted",
                user.open_id,
                table_id,
            )
        workspace = replace(workspace, bootstrap_table_id="")
        self.store.save(workspace)
        return workspace


class BitableArchiver:
    """Writes business results into each user's own workspace, deduplicated
    by a stable archive_key."""

    def __init__(
        self,
        store: WorkspaceStore,
        client_for_user: Callable[[User], BitableClient],
        timezone: ZoneInfo,
        runtime_store: RuntimeStore | None = None,
        *,
        include_meeting: bool = True,
    ) -> None:
        self.store = store
        self.client_for_user = client_for_user
        self.timezone = timezone
        self.runtime_store = runtime_store
        self.include_meeting = include_meeting
        self._managers: dict[str, WorkspaceManager] = {}

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
        record = message_digest_record(
            summary, start_time, end_time, message_count, source_names, self.timezone
        )
        return self._store_record(
            user, MESSAGE_DIGEST_TABLE, "message_table_id", archive_key, record
        )

    def archive_meeting_minutes(
        self,
        user: User,
        archive_key: str,
        minutes: MeetingMinutes,
        *,
        recording: Recording,
        message_id: str = "",
    ) -> str:
        record = meeting_minutes_record(minutes, recording, self.timezone)
        return self._store_record(
            user, MEETING_MINUTES_TABLE, "meeting_table_id", archive_key, record
        )

    def _manager(self, user: User) -> WorkspaceManager:
        manager = self._managers.get(user.open_id)
        if manager is None:
            manager = WorkspaceManager(
                self.client_for_user(user),
                self.store,
                self.runtime_store,
                include_meeting=self.include_meeting,
            )
            self._managers[user.open_id] = manager
        return manager

    def _store_record(
        self,
        user: User,
        spec: WorkspaceTableSpec,
        attribute: str,
        archive_key: str,
        record: dict[str, object],
    ) -> str:
        logger = logging.getLogger(__name__)
        if not archive_key.strip():
            raise ValueError("archive_key must not be empty")
        if self.runtime_store is not None:
            return self._store_record_cached(
                user, spec, attribute, archive_key, record
            )
        lock_path = self.store.path.with_name(f"{self.store.path.name}.lock")
        with single_instance_lock(lock_path):
            manager = self._manager(user)
            for attempt in range(2):
                workspace = manager.ensure_workspace(user, validate_remote=False)
                table_id = str(getattr(workspace, attribute))
                client = manager.client
                try:
                    existing = client.find_record_by_archive_key(
                        workspace.app_token, table_id, archive_key
                    )
                    if existing:
                        logger.info(
                            "workspace user=%s table=%s "
                            "stage=archive status=deduplicated record=%s",
                            user.open_id,
                            spec.name,
                            existing,
                        )
                        return existing
                    fields = dict(record)
                    fields[ARCHIVE_KEY_FIELD] = archive_key
                    record_id = client.create_record(
                        workspace.app_token, table_id, fields
                    )
                except BitableNotFoundError:
                    if attempt:
                        raise
                    manager.invalidate()
                    continue
                logger.info(
                    "workspace user=%s table=%s stage=archive status=created record=%s",
                    user.open_id,
                    spec.name,
                    record_id,
                )
                return record_id
        raise AssertionError("unreachable archive retry state")

    def _store_record_cached(
        self,
        user: User,
        spec: WorkspaceTableSpec,
        attribute: str,
        archive_key: str,
        record: dict[str, object],
    ) -> str:
        runtime = self.runtime_store
        if runtime is None:
            raise AssertionError("runtime store is required")
        logger = logging.getLogger(__name__)
        with runtime.lock(f"archive:{archive_key}"):
            for attempt in range(2):
                manager = self._manager(user)
                workspace = manager.ensure_workspace(user, validate_remote=False)
                table_id = str(getattr(workspace, attribute))
                client = manager.client
                try:
                    self._ensure_archive_cache(
                        runtime, user, spec, workspace, table_id, client
                    )
                    state = runtime.archive_state(archive_key)
                    if state is not None and state.status == "succeeded":
                        return state.record_id
                    if state is not None:
                        existing = client.find_record_by_archive_key(
                            workspace.app_token, table_id, archive_key
                        )
                        if existing:
                            runtime.mark_archive(
                                archive_key,
                                user.open_id,
                                spec.key,
                                "succeeded",
                                existing,
                            )
                            return existing
                    runtime.mark_archive(
                        archive_key, user.open_id, spec.key, "creating"
                    )
                    fields = dict(record)
                    fields[ARCHIVE_KEY_FIELD] = archive_key
                    try:
                        record_id = client.create_record(
                            workspace.app_token, table_id, fields
                        )
                    except FeishuAPIError:
                        runtime.mark_archive(
                            archive_key, user.open_id, spec.key, "uncertain"
                        )
                        raise
                    runtime.mark_archive(
                        archive_key,
                        user.open_id,
                        spec.key,
                        "succeeded",
                        record_id,
                    )
                    logger.info(
                        "workspace user=%s table=%s stage=archive "
                        "status=created record=%s",
                        user.open_id,
                        spec.name,
                        record_id,
                    )
                    return record_id
                except BitableNotFoundError:
                    runtime.invalidate_workspace(user.open_id)
                    manager.invalidate()
                    if attempt:
                        raise
        raise AssertionError("unreachable cached archive retry state")

    @staticmethod
    def _ensure_archive_cache(
        runtime: RuntimeStore,
        user: User,
        spec: WorkspaceTableSpec,
        workspace: UserWorkspace,
        table_id: str,
        client: BitableClient,
    ) -> None:
        if runtime.archive_cache_current(
            user.open_id, spec.key, workspace.app_token, table_id
        ):
            return
        with runtime.lock(f"archive-cache:{user.open_id}:{spec.key}"):
            if runtime.archive_cache_current(
                user.open_id, spec.key, workspace.app_token, table_id
            ):
                return
            records = client.list_archive_records(workspace.app_token, table_id)
            runtime.initialize_archive_cache(
                user.open_id,
                spec.key,
                workspace.app_token,
                table_id,
                records,
            )
