from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import CheckpointError


def endpoint_name(
    method: str, path: str, params: Mapping[str, object] | None = None
) -> str:
    """Return a stable local metric name for one Feishu HTTP request."""
    normalized = path.removeprefix("/open-apis")
    method = method.upper()
    fixed = {
        "/auth/v3/app_access_token/internal": "auth.app_token",
        "/auth/v3/tenant_access_token/internal": "auth.tenant_token",
        "/authen/v1/access_token": "auth.access_token",
        "/authen/v1/refresh_access_token": "auth.refresh",
        "/authen/v1/user_info": "auth.user_info",
        "/im/v1/chats": "im.chats.list",
    }
    if normalized in fixed:
        return fixed[normalized]
    if normalized.endswith("/members") and "/im/v1/chats/" in normalized:
        return "im.chat_members.list"
    if normalized == "/im/v1/messages":
        return "im.messages.list" if method == "GET" else "im.message.create"
    if normalized.startswith("/minutes/v1/minutes/") and normalized.endswith(
        "/media"
    ):
        return "minutes.media.get"
    if normalized.startswith("/vc/v1/meetings/") and normalized.endswith(
        "/recording"
    ):
        return "vc.recording.get"
    if normalized.startswith("/vc/v1/meetings/"):
        return "vc.meeting.get"
    if "/records" in normalized and "/base/v3/bases/" in normalized:
        return (
            "bitable.record.search"
            if params and "filter" in params
            else "bitable.record.list"
        )
    if "/records" in normalized and "/bitable/v1/apps/" in normalized:
        return "bitable.record.create" if method == "POST" else "bitable.record.list"
    if normalized == "/bitable/v1/apps":
        return "bitable.app.create"
    if normalized.startswith("/bitable/v1/apps/"):
        if normalized.endswith("/tables"):
            return "bitable.table.create" if method == "POST" else "bitable.table.list"
        if normalized.endswith("/fields"):
            return "bitable.field.create" if method == "POST" else "bitable.field.list"
        if "/tables/" not in normalized:
            return "bitable.app.get"
        if method == "DELETE":
            return "bitable.table.delete"
    if "/views" in normalized:
        return (
            "bitable.view.list"
            if normalized.endswith("/views")
            else "bitable.view.configure"
        )
    return f"{method.lower()} {normalized}"


@dataclass(frozen=True)
class APIUsageSummary:
    start_timestamp: int
    end_timestamp: int
    total: int
    failures: int
    by_endpoint: dict[str, int]
    by_user: dict[str, int]
    by_caller: dict[str, int]


class APICallRecorder:
    """Persist local observations for real Feishu HTTP requests."""

    def __init__(self, database_path: Path, caller: str) -> None:
        self.database_path = database_path
        self.caller = caller.strip() or "unknown"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS api_call_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        endpoint TEXT NOT NULL,
                        user_open_id TEXT NOT NULL,
                        caller TEXT NOT NULL,
                        timestamp INTEGER NOT NULL,
                        success INTEGER NOT NULL CHECK (success IN (0, 1))
                    );
                    CREATE INDEX IF NOT EXISTS api_call_events_timestamp
                    ON api_call_events(timestamp);
                    CREATE INDEX IF NOT EXISTS api_call_events_user_timestamp
                    ON api_call_events(user_open_id, timestamp);
                    """
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to initialize API usage store: {exc}") from exc

    def observe(
        self,
        method: str,
        path: str,
        user_open_id: str,
        success: bool,
        params: Mapping[str, object] | None = None,
    ) -> None:
        now = int(time.time())
        endpoint = endpoint_name(method, path, params)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO api_call_events(
                        endpoint, user_open_id, caller, timestamp, success
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (endpoint, user_open_id, self.caller, now, int(success)),
                )
                if now % 97 == 0:
                    connection.execute(
                        "DELETE FROM api_call_events WHERE timestamp < ?",
                        (now - 400 * 86400,),
                    )
        except sqlite3.Error as exc:
            # Observability must never turn an already successful remote mutation
            # into a business failure (and possibly cause a duplicate retry).
            logging.getLogger(__name__).warning(
                "unable to record local API usage endpoint=%s caller=%s: %s",
                endpoint,
                self.caller,
                exc,
            )

    def summarize(
        self,
        *,
        start_timestamp: int,
        end_timestamp: int,
        user_open_id: str = "",
        caller: str = "",
    ) -> APIUsageSummary:
        clauses = ["timestamp >= ?", "timestamp < ?"]
        values: list[object] = [start_timestamp, end_timestamp]
        if user_open_id:
            clauses.append("user_open_id = ?")
            values.append(user_open_id)
        if caller:
            clauses.append("caller = ?")
            values.append(caller)
        where = " AND ".join(clauses)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT endpoint, user_open_id, caller, success "
                    f"FROM api_call_events WHERE {where}",
                    values,
                ).fetchall()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to summarize API usage: {exc}") from exc
        by_endpoint: dict[str, int] = {}
        by_user: dict[str, int] = {}
        by_caller: dict[str, int] = {}
        failures = 0
        for row in rows:
            endpoint = str(row["endpoint"])
            user = str(row["user_open_id"] or "(application)")
            task = str(row["caller"])
            by_endpoint[endpoint] = by_endpoint.get(endpoint, 0) + 1
            by_user[user] = by_user.get(user, 0) + 1
            by_caller[task] = by_caller.get(task, 0) + 1
            failures += int(row["success"]) == 0
        return APIUsageSummary(
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            total=len(rows),
            failures=failures,
            by_endpoint=dict(sorted(by_endpoint.items())),
            by_user=dict(sorted(by_user.items())),
            by_caller=dict(sorted(by_caller.items())),
        )


def usage_range(
    *, day: str = "", month: str = "", timezone: ZoneInfo
) -> tuple[int, int]:
    if day and month:
        raise ValueError("day and month cannot be used together")
    if day:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone)
        return int(start.timestamp()), int(start.timestamp()) + 86400
    if month:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return int(start.timestamp()), int(end.timestamp())
    now = datetime.now(timezone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(start.timestamp()) + 86400
