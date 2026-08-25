from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConversationType(StrEnum):
    GROUP = "group"
    PRIVATE = "private"


@dataclass(frozen=True)
class User:
    open_id: str
    name: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.open_id.strip():
            raise ValueError("user open_id must not be empty")
        if not self.name.strip():
            raise ValueError("user name must not be empty")


@dataclass(frozen=True)
class Conversation:
    chat_id: str
    name: str
    conversation_type: ConversationType
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.chat_id.strip():
            raise ValueError("conversation chat_id must not be empty")
        if not self.name.strip():
            raise ValueError("conversation name must not be empty")
