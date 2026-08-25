from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .domain import Conversation, ConversationType, User
from .errors import FeishuAPIError, MediaDownloadError
from .feishu_client import FeishuClient, FeishuMessage, Recording, RecordingFile
from .runtime_store import RuntimeStore

JSONRunner = Callable[[list[str]], dict[str, object]]


@dataclass(frozen=True)
class MinutesTriggerMessage:
    message_id: str
    create_time: int
    minute_token: str
    url: str
    source_name: str
    source_time: str = ""


class UserIdentityClient:
    """User-identity message access backed by lark-cli's OAuth token store.

    Each lark-cli profile owns one user's access/refresh token pair and performs
    refresh automatically before an API call. The summary business flow only
    sees User, Conversation, and FeishuMessage values.
    """

    def __init__(
        self,
        expected_open_id: str,
        profile: str = "",
        runner: JSONRunner | None = None,
        http: httpx.Client | None = None,
        runtime_store: RuntimeStore | None = None,
    ) -> None:
        if not expected_open_id.strip():
            raise ValueError("expected user open_id must not be empty")
        self.expected_open_id = expected_open_id
        self.profile = profile.strip()
        self._runner = runner or self._run_json
        self._http = http
        self._member_names: dict[str, dict[str, str]] = {}
        self._minutes_assistant_chat_id = ""
        self.runtime_store = runtime_store
        self._conversation_hints: dict[str, Conversation] = {}
        self._user_name = ""

    def set_conversation_context(
        self, user: User, conversations: list[Conversation]
    ) -> None:
        self._user_name = user.name
        self._conversation_hints = {
            conversation.chat_id: conversation for conversation in conversations
        }
        if self.runtime_store is not None:
            self.runtime_store.save_names({user.open_id: user.name})

    def resolve_user(self) -> User:
        payload = self._call(["whoami", "--as", "user"])
        identity = payload.get("onBehalfOf")
        if not isinstance(identity, dict):
            raise FeishuAPIError("user OAuth identity returned no user information")
        open_id = identity.get("openId")
        name = identity.get("userName")
        if open_id != self.expected_open_id:
            raise FeishuAPIError(
                "OAuth profile user does not match configured user open_id"
            )
        if not isinstance(name, str) or not name.strip():
            raise FeishuAPIError("user OAuth identity returned an empty user name")
        return User(open_id=open_id, name=name.strip())

    def get_recording(self, meeting_id: str) -> Recording:
        meeting_id = meeting_id.strip()
        if not meeting_id:
            raise ValueError("meeting_id must not be empty")
        payload = self._call(
            [
                "vc",
                "+recording",
                "--as",
                "user",
                "--meeting-ids",
                meeting_id,
                "--format",
                "json",
            ]
        )
        data = payload.get("data")
        recordings = data.get("recordings", []) if isinstance(data, dict) else []
        if not isinstance(recordings, list):
            raise FeishuAPIError("user recording API returned invalid recordings")
        match = next(
            (
                item
                for item in recordings
                if isinstance(item, dict) and item.get("meeting_id") == meeting_id
            ),
            None,
        )
        if match is None:
            raise FeishuAPIError("user recording API returned no recording")
        minute_token = match.get("minute_token")
        url = match.get("recording_url")
        if not isinstance(minute_token, str) or not minute_token:
            raise FeishuAPIError("user recording API returned an empty minute_token")
        if not isinstance(url, str) or not url:
            raise FeishuAPIError("user recording API returned an empty recording_url")
        detail_payload = self._call(
            [
                "vc",
                "+detail",
                "--as",
                "user",
                "--meeting-ids",
                meeting_id,
                "--format",
                "json",
            ]
        )
        detail_data = detail_payload.get("data")
        meetings = (
            detail_data.get("meetings", []) if isinstance(detail_data, dict) else []
        )
        detail = next(
            (
                item
                for item in meetings
                if isinstance(item, dict) and item.get("meeting_id") == meeting_id
            ),
            None,
        )
        topic = detail.get("topic") if isinstance(detail, dict) else None
        start_time = detail.get("start_time") if isinstance(detail, dict) else None
        source_name = (
            topic.strip() if isinstance(topic, str) and topic.strip() else meeting_id
        )
        source_time = (
            start_time.strip()
            if isinstance(start_time, str) and start_time.strip()
            else ""
        )
        return Recording(
            meeting_id=meeting_id,
            duration=str(match.get("duration", "")),
            url=url,
            minute_token=minute_token,
            source_name=source_name,
            source_time=source_time,
        )

    def download_recording(
        self, recording: Recording, output_dir: Path
    ) -> RecordingFile:
        payload = self._call(
            [
                "minutes",
                "+download",
                "--as",
                "user",
                "--minute-tokens",
                recording.minute_token,
                "--url-only",
                "--format",
                "json",
            ]
        )
        data = payload.get("data")
        download_url = data.get("download_url") if isinstance(data, dict) else None
        if not isinstance(download_url, str) or not download_url:
            raise FeishuAPIError("user minutes API returned an empty download_url")
        parsed = urlparse(download_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise FeishuAPIError("user minutes API returned an invalid download_url")
        return self._download_media(download_url, recording, output_dir)

    def _download_media(
        self, download_url: str, recording: Recording, output_dir: Path
    ) -> RecordingFile:
        client = self._http or httpx.Client(
            timeout=httpx.Timeout(30.0, read=1800.0), follow_redirects=True
        )
        try:
            with client.stream("GET", download_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                disposition = response.headers.get("content-disposition", "")
                filename = FeishuClient._recording_filename(
                    disposition, content_type, recording.minute_token
                )
                output_dir.mkdir(parents=True, exist_ok=True)
                destination = output_dir / filename
                temporary = output_dir / f".{filename}.part"
                size = 0
                try:
                    with temporary.open("wb") as output:
                        for chunk in response.iter_bytes():
                            output.write(chunk)
                            size += len(chunk)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
        except (httpx.HTTPError, OSError) as exc:
            raise MediaDownloadError(f"recording media download failed: {exc}") from exc
        finally:
            if self._http is None:
                client.close()
        if size == 0:
            destination.unlink(missing_ok=True)
            raise MediaDownloadError("recording media download returned an empty file")
        return RecordingFile(
            path=destination.resolve(),
            content_type=content_type,
            size_bytes=size,
            source_url=download_url,
        )

    def list_minutes_triggers(
        self, start_time_ms: int, end_time_ms: int
    ) -> list[MinutesTriggerMessage]:
        if start_time_ms > end_time_ms:
            raise ValueError("start_time_ms must not be after end_time_ms")
        chat_id = self._get_minutes_assistant_chat_id()
        params = json.dumps(
            {
                "container_id_type": "chat",
                "container_id": chat_id,
                "start_time": str(max(start_time_ms // 1000 - 1, 0)),
                "end_time": str(end_time_ms // 1000 + 1),
                "sort_type": "ByCreateTimeAsc",
                "page_size": "50",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = self._call(
            [
                "api",
                "GET",
                "/open-apis/im/v1/messages",
                "--as",
                "user",
                "--params",
                params,
                "--page-all",
                "--page-limit",
                "100",
                "--format",
                "json",
            ]
        )
        data = payload.get("data")
        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            raise FeishuAPIError("minutes assistant returned invalid messages")
        triggers: list[MinutesTriggerMessage] = []
        for item in items:
            trigger = self._parse_minutes_trigger(item)
            if (
                trigger is not None
                and start_time_ms < trigger.create_time <= end_time_ms
            ):
                triggers.append(trigger)
        return sorted(
            triggers, key=lambda trigger: (trigger.create_time, trigger.message_id)
        )

    def _get_minutes_assistant_chat_id(self) -> str:
        if self._minutes_assistant_chat_id:
            return self._minutes_assistant_chat_id
        if self.runtime_store is not None:
            cached = self.runtime_store.resource(
                self.expected_open_id, "minutes_assistant_chat_id"
            )
            if cached:
                self._minutes_assistant_chat_id = cached
                return cached
            with self.runtime_store.lock(
                f"minutes-assistant-chat:{self.expected_open_id}"
            ):
                cached = self.runtime_store.resource(
                    self.expected_open_id, "minutes_assistant_chat_id"
                )
                if cached:
                    self._minutes_assistant_chat_id = cached
                    return cached
                discovered = self._discover_minutes_assistant_chat_id()
                self.runtime_store.save_resource(
                    self.expected_open_id,
                    "minutes_assistant_chat_id",
                    discovered,
                )
                self._minutes_assistant_chat_id = discovered
                return discovered
        self._minutes_assistant_chat_id = self._discover_minutes_assistant_chat_id()
        return self._minutes_assistant_chat_id

    def _discover_minutes_assistant_chat_id(self) -> str:
        payload = self._call(
            [
                "im",
                "+chat-list",
                "--as",
                "user",
                "--types=p2p,group",
                "--sort",
                "active_time",
                "--page-all",
                "--format",
                "json",
            ]
        )
        data = payload.get("data")
        chats = data.get("chats", []) if isinstance(data, dict) else []
        matches = [
            item
            for item in chats
            if isinstance(item, dict)
            and item.get("chat_mode") == "p2p"
            and item.get("p2p_target_type") == "bot"
            and item.get("name") == "智能纪要助手"
            and isinstance(item.get("chat_id"), str)
        ]
        if len(matches) != 1:
            raise FeishuAPIError(
                "unable to uniquely discover the 智能纪要助手 conversation"
            )
        return str(matches[0]["chat_id"])

    @staticmethod
    def _parse_minutes_trigger(item: object) -> MinutesTriggerMessage | None:
        if not isinstance(item, dict) or item.get("msg_type") != "interactive":
            return None
        message_id = item.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return None
        try:
            create_time = int(str(item.get("create_time", "0")))
        except ValueError:
            return None
        body = item.get("body")
        raw_content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(raw_content, str):
            return None
        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            return None
        if not isinstance(content, dict) or content.get("title") not in {
            "会议录制已完成",
            "妙记已生成",
        }:
            return None
        links: list[tuple[str, str]] = []
        text_blocks: list[str] = []

        def collect(value: object) -> None:
            if isinstance(value, dict):
                text_value = value.get("text")
                if value.get("tag") == "text" and isinstance(text_value, str):
                    text_blocks.append(text_value.strip())
                href = value.get("href")
                if value.get("tag") == "a" and isinstance(href, str):
                    text = value.get("text")
                    links.append((href, text if isinstance(text, str) else ""))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(content.get("elements"))
        source_time = next(
            (
                text.removeprefix("日期：").strip()
                for text in text_blocks
                if text.startswith("日期：")
            ),
            "",
        )
        for url, text in links:
            minute_token = FeishuClient.extract_minute_token(url)
            if minute_token:
                return MinutesTriggerMessage(
                    message_id=message_id,
                    create_time=create_time,
                    minute_token=minute_token,
                    url=url,
                    source_name=text.strip() or str(content.get("title")),
                    source_time=source_time,
                )
        return None

    def discover_conversations(self, user: User) -> list[Conversation]:
        payload = self._call(
            [
                "im",
                "+chat-list",
                "--as",
                "user",
                "--types=p2p,group",
                "--sort",
                "active_time",
                "--page-all",
                "--format",
                "json",
            ]
        )
        data = payload.get("data")
        chats = data.get("chats", []) if isinstance(data, dict) else []
        if not isinstance(chats, list):
            raise FeishuAPIError("user chat list returned invalid chats")

        conversations: list[Conversation] = []
        for item in chats:
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            name = item.get("name")
            mode = item.get("chat_mode")
            if not isinstance(chat_id, str) or not chat_id:
                continue
            if not isinstance(name, str) or not name.strip():
                name = chat_id
            if mode == "p2p":
                target_type = item.get("p2p_target_type")
                if target_type != "user":
                    continue
                conversation_type = ConversationType.PRIVATE
            elif mode == "group":
                conversation_type = ConversationType.GROUP
            else:
                continue
            conversations.append(
                Conversation(
                    chat_id=chat_id,
                    name=name.strip(),
                    conversation_type=conversation_type,
                    enabled=False,
                )
            )
        return conversations

    def list_messages(
        self, chat_id: str, start_time: int, end_time: int
    ) -> list[FeishuMessage]:
        params = json.dumps(
            {
                "container_id_type": "chat",
                "container_id": chat_id,
                "start_time": str(start_time),
                "end_time": str(end_time),
                "sort_type": "ByCreateTimeAsc",
                "page_size": "50",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = self._call(
            [
                "api",
                "GET",
                "/open-apis/im/v1/messages",
                "--as",
                "user",
                "--params",
                params,
                "--page-all",
                "--page-limit",
                "100",
                "--format",
                "json",
            ]
        )
        data = payload.get("data")
        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            raise FeishuAPIError("user message API returned invalid items")
        if not items:
            return []
        sender_ids = {
            str(sender.get("id"))
            for item in items
            if isinstance(item, dict)
            and isinstance((sender := item.get("sender")), dict)
            and sender.get("id")
        }
        member_names = (
            self.runtime_store.names(sender_ids)
            if self.runtime_store is not None
            else {}
        )
        if self._user_name:
            member_names.setdefault(self.expected_open_id, self._user_name)
        unknown = sender_ids - set(member_names)
        hint = self._conversation_hints.get(chat_id)
        inferred_names: dict[str, str] = {}
        if hint is not None and hint.conversation_type == ConversationType.PRIVATE:
            counterparts = unknown - {self.expected_open_id}
            if len(counterparts) == 1:
                counterpart = next(iter(counterparts))
                member_names[counterpart] = hint.name
                inferred_names[counterpart] = hint.name
                unknown -= counterparts
        if unknown:
            discovered_names = self._get_member_names(chat_id)
            member_names.update(discovered_names)
            if self.runtime_store is not None:
                self.runtime_store.save_names(discovered_names)
        elif self.runtime_store is not None and inferred_names:
            self.runtime_store.save_names(inferred_names)
        messages: list[FeishuMessage] = []
        seen: set[str] = set()
        for item in items:
            message = FeishuClient._parse_message(item, chat_id, member_names)
            if message and message.message_id not in seen:
                seen.add(message.message_id)
                messages.append(message)
        return sorted(
            messages, key=lambda message: (message.create_time, message.message_id)
        )

    def _get_member_names(self, chat_id: str) -> dict[str, str]:
        if chat_id in self._member_names:
            return self._member_names[chat_id]
        payload = self._call(
            [
                "api",
                "GET",
                f"/open-apis/im/v1/chats/{chat_id}/members",
                "--as",
                "user",
                "--params",
                '{"member_id_type":"open_id","page_size":"100"}',
                "--page-all",
                "--page-limit",
                "100",
                "--format",
                "json",
            ]
        )
        data = payload.get("data")
        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            raise FeishuAPIError("user chat member API returned invalid items")
        names: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            member_id = item.get("member_id")
            name = item.get("name")
            if isinstance(member_id, str) and isinstance(name, str) and name.strip():
                names[member_id] = name.strip()
        self._member_names[chat_id] = names
        return names

    def _call(self, args: list[str]) -> dict[str, object]:
        command = ["lark-cli"]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend(args)
        payload = self._runner(command)
        if payload.get("ok") is False:
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message", "unknown lark-cli error")
            else:
                message = "unknown lark-cli error"
            raise FeishuAPIError(f"user identity request failed: {message}")
        return payload

    @staticmethod
    def _run_json(command: list[str]) -> dict[str, object]:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FeishuAPIError(f"failed to execute lark-cli: {exc}") from exc
        output = result.stdout.strip() or result.stderr.strip()
        try:
            payload: Any = json.loads(output)
        except json.JSONDecodeError as exc:
            raise FeishuAPIError("lark-cli returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise FeishuAPIError("lark-cli returned a non-object response")
        if result.returncode != 0 and payload.get("ok") is not False:
            raise FeishuAPIError(f"lark-cli exited with status {result.returncode}")
        return payload
