from __future__ import annotations

from pathlib import Path

import pytest

from feishu_assistant import cli
from feishu_assistant.conversation_preferences import ConversationPreferenceStore
from feishu_assistant.domain import Conversation, ConversationType, User


def _conversation(
    chat_id: str,
    name: str,
    kind: ConversationType,
) -> Conversation:
    return Conversation(chat_id, name, kind, enabled=False)


def test_new_conversations_are_disabled_and_existing_choice_is_preserved(
    tmp_path: Path,
) -> None:
    store = ConversationPreferenceStore(tmp_path / "oauth.sqlite3")
    group = _conversation("oc_group", "研发群", ConversationType.GROUP)
    private = _conversation("oc_private", "测试同事", ConversationType.PRIVATE)

    first = store.sync_discovered("ou_user", [group, private])
    store.save_enabled(
        "ou_user",
        {group.chat_id, private.chat_id},
        {group.chat_id},
    )
    second = store.sync_discovered(
        "ou_user",
        [
            _conversation("oc_group", "研发协作群", ConversationType.GROUP),
            private,
            _conversation("oc_new", "产品讨论群", ConversationType.GROUP),
        ],
    )

    assert all(not item.enabled for item in first)
    assert [(item.name, item.enabled) for item in second] == [
        ("研发协作群", True),
        ("测试同事", False),
        ("产品讨论群", False),
    ]


def test_temporarily_missing_conversation_is_not_deleted_or_reset(
    tmp_path: Path,
) -> None:
    store = ConversationPreferenceStore(tmp_path / "oauth.sqlite3")
    group = _conversation("oc_group", "研发群", ConversationType.GROUP)
    private = _conversation("oc_private", "测试同事", ConversationType.PRIVATE)
    store.sync_discovered("ou_user", [group, private])
    store.save_enabled(
        "ou_user",
        {group.chat_id, private.chat_id},
        {private.chat_id},
    )

    store.sync_discovered("ou_user", [group])
    reappeared = store.sync_discovered("ou_user", [group, private])

    assert [(item.chat_id, item.enabled) for item in reappeared] == [
        ("oc_group", False),
        ("oc_private", True),
    ]
    assert len(store.list_for_user("ou_user")) == 2


def test_preferences_are_isolated_by_user(tmp_path: Path) -> None:
    store = ConversationPreferenceStore(tmp_path / "oauth.sqlite3")
    group = _conversation("oc_shared", "研发群", ConversationType.GROUP)
    store.sync_discovered("ou_a", [group])
    store.sync_discovered("ou_b", [group])
    store.save_enabled("ou_a", {group.chat_id}, {group.chat_id})

    assert store.sync_discovered("ou_a", [group])[0].enabled is True
    assert store.sync_discovered("ou_b", [group])[0].enabled is False


def test_digest_configuration_uses_only_saved_enabled_conversations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConversationPreferenceStore(tmp_path / "oauth.sqlite3")
    group = _conversation("oc_group", "技术开发测试", ConversationType.GROUP)
    private = _conversation("oc_private", "测试同事", ConversationType.PRIVATE)
    new_group = _conversation("oc_new", "新项目群", ConversationType.GROUP)
    discovered = [group, private]
    captured: list[list[str]] = []

    class Reader:
        def resolve_user(self) -> User:
            return User("ou_user", "测试用户")

        def discover_conversations(self, user: User) -> list[Conversation]:
            assert user.open_id == "ou_user"
            return discovered

    def build_digest(
        reader: object,
        sender: object,
        user: User,
        conversations: list[Conversation],
        checkpoint_scope: str,
        archiver: object,
    ) -> object:
        captured.append([item.chat_id for item in conversations])
        return object()

    monkeypatch.setattr(cli, "_configured_open_ids", lambda: ["ou_user"])
    monkeypatch.setattr(cli, "_user_identity_client", lambda _: Reader())
    monkeypatch.setattr(cli, "_conversation_preference_store", lambda: store)
    monkeypatch.setattr(cli, "_message_digest", build_digest)
    monkeypatch.setattr(cli, "message_read_identity_from_env", lambda: "user")
    monkeypatch.setattr(cli, "message_retry_attempts_from_env", lambda: 1)

    configured = cli._configured_digests(  # type: ignore[arg-type]
        object(), object()
    )
    assert len(configured) == 1
    assert captured[-1] == []

    store.save_enabled(
        "ou_user",
        {group.chat_id, private.chat_id},
        {group.chat_id, private.chat_id},
    )
    configured = cli._configured_digests(  # type: ignore[arg-type]
        object(), object()
    )
    assert len(configured) == 1
    assert captured[-1] == ["oc_group", "oc_private"]

    discovered[:] = [group, private, new_group]
    store.save_enabled(
        "ou_user",
        {group.chat_id, private.chat_id},
        {group.chat_id},
    )
    cli._configured_digests(object(), object())  # type: ignore[arg-type]

    assert captured[-1] == ["oc_group"]
    preferences = {item.chat_id: item.enabled for item in store.list_for_user("ou_user")}
    assert preferences["oc_new"] is False


def test_oauth_digest_reuses_saved_identity_and_does_not_rediscover_chats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConversationPreferenceStore(tmp_path / "oauth.sqlite3")
    group = _conversation("oc_group", "技术开发测试", ConversationType.GROUP)
    store.sync_discovered("ou_user", [group])
    store.save_enabled("ou_user", {group.chat_id}, {group.chat_id})
    readers: list[object] = []
    captured: list[list[str]] = []

    class OAuthUsers:
        @staticmethod
        def users() -> list[User]:
            return [User("ou_user", "测试用户")]

    class Reader:
        def resolve_user(self) -> User:
            raise AssertionError("stored OAuth identity must be reused")

        def discover_conversations(self, user: User) -> list[Conversation]:
            raise AssertionError("scheduled digest must not scan disabled chats")

    def reader(_: str) -> Reader:
        value = Reader()
        readers.append(value)
        return value

    def build_digest(
        reader: object,
        sender: object,
        user: User,
        conversations: list[Conversation],
        checkpoint_scope: str,
        archiver: object,
    ) -> object:
        captured.append([item.chat_id for item in conversations])
        return object()

    monkeypatch.setattr(cli, "_configured_open_ids", lambda: ["ou_user"])
    monkeypatch.setattr(cli, "_oauth_store", lambda: OAuthUsers())
    monkeypatch.setattr(cli, "_user_identity_client", reader)
    monkeypatch.setattr(cli, "_conversation_preference_store", lambda: store)
    monkeypatch.setattr(cli, "_message_digest", build_digest)
    monkeypatch.setattr(cli, "message_read_identity_from_env", lambda: "user")
    monkeypatch.setattr(cli, "message_retry_attempts_from_env", lambda: 1)

    cache: dict[str, object] = {}
    cli._configured_digests(  # type: ignore[arg-type]
        object(), object(), reader_cache=cache
    )
    cli._configured_digests(  # type: ignore[arg-type]
        object(), object(), reader_cache=cache
    )

    assert captured == [["oc_group"], ["oc_group"]]
    assert len(readers) == 1
