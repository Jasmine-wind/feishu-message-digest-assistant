from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .domain import Conversation, ConversationType
from .errors import CheckpointError


@dataclass(frozen=True)
class ConversationPreference:
    user_open_id: str
    chat_id: str
    chat_name: str
    conversation_type: ConversationType
    enabled: bool
    updated_at: int


class ConversationPreferenceStore:
    """Per-user conversation choices stored beside OAuth credentials in SQLite."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_preferences (
                        user_open_id TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        chat_name TEXT NOT NULL,
                        conversation_type TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (user_open_id, chat_id),
                        CHECK (conversation_type IN ('group', 'private')),
                        CHECK (enabled IN (0, 1))
                    )
                    """
                )
            os.chmod(self.database_path, 0o600)
        except OSError as exc:
            raise CheckpointError(
                f"unable to initialize conversation preferences: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise CheckpointError(
                f"invalid conversation preference database: {exc}"
            ) from exc

    def sync_discovered(
        self,
        user_open_id: str,
        discovered: list[Conversation],
    ) -> list[Conversation]:
        """Add new chats disabled and refresh names without changing choices."""
        if not user_open_id.strip():
            raise ValueError("user open_id must not be empty")
        unique = list({item.chat_id: item for item in discovered}.values())
        now = int(time.time())
        try:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO conversation_preferences(
                        user_open_id, chat_id, chat_name,
                        conversation_type, enabled, updated_at
                    ) VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(user_open_id, chat_id) DO UPDATE SET
                        chat_name = excluded.chat_name,
                        conversation_type = excluded.conversation_type,
                        updated_at = CASE
                            WHEN chat_name != excluded.chat_name
                              OR conversation_type != excluded.conversation_type
                            THEN excluded.updated_at
                            ELSE updated_at
                        END
                    """,
                    [
                        (
                            user_open_id,
                            conversation.chat_id,
                            conversation.name,
                            conversation.conversation_type.value,
                            now,
                        )
                        for conversation in unique
                    ],
                )
                rows = connection.execute(
                    """
                    SELECT chat_id, enabled
                    FROM conversation_preferences
                    WHERE user_open_id = ?
                    """,
                    (user_open_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise CheckpointError(
                f"unable to sync conversation preferences: {exc}"
            ) from exc
        enabled = {str(row["chat_id"]): bool(row["enabled"]) for row in rows}
        return [
            Conversation(
                chat_id=conversation.chat_id,
                name=conversation.name,
                conversation_type=conversation.conversation_type,
                enabled=enabled.get(conversation.chat_id, False),
            )
            for conversation in unique
        ]

    def save_enabled(
        self,
        user_open_id: str,
        visible_chat_ids: set[str],
        enabled_chat_ids: set[str],
    ) -> None:
        unknown = enabled_chat_ids - visible_chat_ids
        if unknown:
            raise ValueError("cannot enable a conversation that is not currently visible")
        now = int(time.time())
        try:
            with self._connect() as connection:
                connection.executemany(
                    """
                    UPDATE conversation_preferences
                    SET enabled = ?, updated_at = ?
                    WHERE user_open_id = ? AND chat_id = ?
                    """,
                    [
                        (
                            int(chat_id in enabled_chat_ids),
                            now,
                            user_open_id,
                            chat_id,
                        )
                        for chat_id in visible_chat_ids
                    ],
                )
        except sqlite3.Error as exc:
            raise CheckpointError(
                f"unable to save conversation preferences: {exc}"
            ) from exc

    def list_for_user(self, user_open_id: str) -> list[ConversationPreference]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT user_open_id, chat_id, chat_name,
                           conversation_type, enabled, updated_at
                    FROM conversation_preferences
                    WHERE user_open_id = ?
                    ORDER BY conversation_type, chat_name, chat_id
                    """,
                    (user_open_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise CheckpointError(
                f"unable to list conversation preferences: {exc}"
            ) from exc
        return [
            ConversationPreference(
                user_open_id=str(row["user_open_id"]),
                chat_id=str(row["chat_id"]),
                chat_name=str(row["chat_name"]),
                conversation_type=ConversationType(str(row["conversation_type"])),
                enabled=bool(row["enabled"]),
                updated_at=int(row["updated_at"]),
            )
            for row in rows
        ]
