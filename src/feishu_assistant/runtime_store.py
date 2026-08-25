from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import CheckpointError
from .reliability import blocking_file_lock


@dataclass(frozen=True)
class ArchiveState:
    archive_key: str
    status: str
    record_id: str


class RuntimeStore:
    """Durable non-secret caches and recovery state shared by all processes."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
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
                    CREATE TABLE IF NOT EXISTS identity_names (
                        open_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS static_resources (
                        user_open_id TEXT NOT NULL,
                        resource_key TEXT NOT NULL,
                        resource_value TEXT NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (user_open_id, resource_key)
                    );
                    CREATE TABLE IF NOT EXISTS workspace_versions (
                        user_open_id TEXT PRIMARY KEY,
                        app_token TEXT NOT NULL,
                        message_table_id TEXT NOT NULL,
                        meeting_table_id TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS archive_table_caches (
                        user_open_id TEXT NOT NULL,
                        table_key TEXT NOT NULL,
                        app_token TEXT NOT NULL,
                        table_id TEXT NOT NULL,
                        initialized_at INTEGER NOT NULL,
                        PRIMARY KEY (user_open_id, table_key)
                    );
                    CREATE TABLE IF NOT EXISTS archive_records (
                        archive_key TEXT PRIMARY KEY,
                        user_open_id TEXT NOT NULL,
                        table_key TEXT NOT NULL,
                        record_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL CHECK (
                            status IN ('creating', 'uncertain', 'succeeded')
                        ),
                        updated_at INTEGER NOT NULL
                    );
                    """
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to initialize runtime store: {exc}") from exc

    @contextmanager
    def lock(self, scope: str) -> Iterator[None]:
        digest = hashlib.sha256(scope.encode()).hexdigest()[:24]
        path = self.database_path.with_name(
            f"{self.database_path.name}.{digest}.lock"
        )
        with blocking_file_lock(path):
            yield

    def names(self, open_ids: set[str]) -> dict[str, str]:
        if not open_ids:
            return {}
        placeholders = ",".join("?" for _ in open_ids)
        values: list[object] = [*sorted(open_ids), int(time.time())]
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT open_id, display_name FROM identity_names
                    WHERE open_id IN ({placeholders}) AND expires_at >= ?
                    """,
                    values,
                ).fetchall()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to load identity names: {exc}") from exc
        return {str(row["open_id"]): str(row["display_name"]) for row in rows}

    def save_names(
        self, names: Mapping[str, str], *, lifetime_seconds: int = 30 * 86400
    ) -> None:
        clean = {
            open_id.strip(): name.strip()
            for open_id, name in names.items()
            if open_id.strip() and name.strip()
        }
        if not clean:
            return
        now = int(time.time())
        try:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO identity_names(
                        open_id, display_name, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(open_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    [
                        (open_id, name, now + lifetime_seconds, now)
                        for open_id, name in clean.items()
                    ],
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to save identity names: {exc}") from exc

    def resource(self, user_open_id: str, key: str) -> str:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT resource_value FROM static_resources
                    WHERE user_open_id = ? AND resource_key = ?
                    """,
                    (user_open_id, key),
                ).fetchone()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to load static resource: {exc}") from exc
        return str(row["resource_value"]) if row is not None else ""

    def save_resource(self, user_open_id: str, key: str, value: str) -> None:
        now = int(time.time())
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO static_resources(
                        user_open_id, resource_key, resource_value, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_open_id, resource_key) DO UPDATE SET
                        resource_value = excluded.resource_value,
                        updated_at = excluded.updated_at
                    """,
                    (user_open_id, key, value, now),
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to save static resource: {exc}") from exc

    def workspace_current(
        self,
        user_open_id: str,
        app_token: str,
        message_table_id: str,
        meeting_table_id: str,
        schema_version: int,
    ) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT app_token, message_table_id, meeting_table_id, schema_version
                    FROM workspace_versions WHERE user_open_id = ?
                    """,
                    (user_open_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to load workspace version: {exc}") from exc
        return row is not None and (
            str(row["app_token"]),
            str(row["message_table_id"]),
            str(row["meeting_table_id"]),
            int(row["schema_version"]),
        ) == (app_token, message_table_id, meeting_table_id, schema_version)

    def save_workspace_version(
        self,
        user_open_id: str,
        app_token: str,
        message_table_id: str,
        meeting_table_id: str,
        schema_version: int,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO workspace_versions(
                        user_open_id, app_token, message_table_id,
                        meeting_table_id, schema_version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_open_id) DO UPDATE SET
                        app_token = excluded.app_token,
                        message_table_id = excluded.message_table_id,
                        meeting_table_id = excluded.meeting_table_id,
                        schema_version = excluded.schema_version,
                        updated_at = excluded.updated_at
                    """,
                    (
                        user_open_id,
                        app_token,
                        message_table_id,
                        meeting_table_id,
                        schema_version,
                        int(time.time()),
                    ),
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to save workspace version: {exc}") from exc

    def invalidate_workspace(self, user_open_id: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM workspace_versions WHERE user_open_id = ?",
                    (user_open_id,),
                )
                connection.execute(
                    "DELETE FROM archive_table_caches WHERE user_open_id = ?",
                    (user_open_id,),
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to invalidate workspace: {exc}") from exc

    def archive_cache_current(
        self,
        user_open_id: str,
        table_key: str,
        app_token: str,
        table_id: str,
    ) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT app_token, table_id FROM archive_table_caches
                    WHERE user_open_id = ? AND table_key = ?
                    """,
                    (user_open_id, table_key),
                ).fetchone()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to load archive cache state: {exc}") from exc
        return row is not None and (
            str(row["app_token"]),
            str(row["table_id"]),
        ) == (app_token, table_id)

    def initialize_archive_cache(
        self,
        user_open_id: str,
        table_key: str,
        app_token: str,
        table_id: str,
        records: Mapping[str, str],
    ) -> None:
        now = int(time.time())
        try:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO archive_records(
                        archive_key, user_open_id, table_key,
                        record_id, status, updated_at
                    ) VALUES (?, ?, ?, ?, 'succeeded', ?)
                    ON CONFLICT(archive_key) DO UPDATE SET
                        record_id = excluded.record_id,
                        status = 'succeeded',
                        updated_at = excluded.updated_at
                    """,
                    [
                        (key, user_open_id, table_key, record_id, now)
                        for key, record_id in records.items()
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO archive_table_caches(
                        user_open_id, table_key, app_token, table_id, initialized_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_open_id, table_key) DO UPDATE SET
                        app_token = excluded.app_token,
                        table_id = excluded.table_id,
                        initialized_at = excluded.initialized_at
                    """,
                    (user_open_id, table_key, app_token, table_id, now),
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to initialize archive cache: {exc}") from exc

    def archive_state(self, archive_key: str) -> ArchiveState | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT archive_key, status, record_id FROM archive_records
                    WHERE archive_key = ?
                    """,
                    (archive_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to load archive state: {exc}") from exc
        if row is None:
            return None
        return ArchiveState(
            archive_key=str(row["archive_key"]),
            status=str(row["status"]),
            record_id=str(row["record_id"]),
        )

    def mark_archive(
        self,
        archive_key: str,
        user_open_id: str,
        table_key: str,
        status: str,
        record_id: str = "",
    ) -> None:
        if status not in {"creating", "uncertain", "succeeded"}:
            raise ValueError("invalid archive status")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO archive_records(
                        archive_key, user_open_id, table_key,
                        record_id, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(archive_key) DO UPDATE SET
                        record_id = excluded.record_id,
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        archive_key,
                        user_open_id,
                        table_key,
                        record_id,
                        status,
                        int(time.time()),
                    ),
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to save archive state: {exc}") from exc
