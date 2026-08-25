from __future__ import annotations

import json
import mimetypes
import os
import time
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Self
from urllib.parse import quote, urlparse

import httpx

from .api_usage import APICallRecorder
from .config import FeishuConfig
from .domain import Conversation, ConversationType, User
from .errors import ConfigurationError, FeishuAPIError, MediaDownloadError


@dataclass(frozen=True)
class Recording:
    meeting_id: str
    duration: str
    url: str
    minute_token: str
    source_name: str = ""
    source_time: str = ""


@dataclass(frozen=True)
class RecordingFile:
    path: Path
    content_type: str
    size_bytes: int
    source_url: str = ""


@dataclass(frozen=True)
class FeishuMessage:
    message_id: str
    chat_id: str
    msg_type: str
    create_time: int
    sender_id: str
    sender_type: str
    text: str
    sender_name: str = ""
    source_name: str = ""


class FeishuClient:
    """The Task 1 subset of Feishu OpenAPI."""

    def __init__(
        self,
        config: FeishuConfig,
        http: httpx.Client | None = None,
        recorder: APICallRecorder | None = None,
    ) -> None:
        self.config = config
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(30.0, read=600.0), follow_redirects=True
        )
        self._owns_http = http is None
        self.recorder = recorder
        self._tenant_token: str | None = None
        self._tenant_token_expires_at = 0.0
        self._chat_member_names: dict[str, dict[str, str]] = {}
        self._chat_names: dict[str, str] = {}
        self._chat_records: list[dict[str, object]] | None = None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_recording(self, meeting_id: str) -> Recording:
        meeting_id = meeting_id.strip()
        if not meeting_id:
            raise ValueError("meeting_id must not be empty")
        data = self._openapi_json(
            "GET",
            f"/vc/v1/meetings/{quote(meeting_id, safe='')}/recording",
            access_token=self.config.user_access_token,
        )
        recording = data.get("recording")
        if not isinstance(recording, dict):
            raise FeishuAPIError("recording API returned no recording")
        url = recording.get("url")
        if not isinstance(url, str) or not url:
            raise FeishuAPIError("recording API returned an empty recording.url")
        minute_token = self.extract_minute_token(url)
        if not minute_token:
            raise FeishuAPIError(
                "recording.url does not contain a /minutes/{minute_token} path"
            )
        return Recording(
            meeting_id=meeting_id,
            duration=str(recording.get("duration", "")),
            url=url,
            minute_token=minute_token,
            source_name=meeting_id,
        )

    @staticmethod
    def extract_minute_token(recording_url: str) -> str:
        parts = [part for part in urlparse(recording_url).path.split("/") if part]
        for index, part in enumerate(parts[:-1]):
            if part == "minutes":
                return parts[index + 1]
        return ""

    def resolve_user(self, open_id: str) -> User:
        for chat in self._list_chat_records():
            chat_id = chat.get("chat_id")
            if not isinstance(chat_id, str):
                continue
            name = self._get_chat_member_names(
                chat_id, self._get_tenant_access_token()
            ).get(open_id)
            if name:
                return User(open_id=open_id, name=name)
        raise FeishuAPIError(
            f"unable to resolve user name for open_id ending in {open_id[-4:]}"
        )

    def discover_conversations(self, user: User) -> list[Conversation]:
        conversations: list[Conversation] = []
        for item in self._list_chat_records():
            if item.get("chat_status") not in {None, "normal"}:
                continue
            chat_id = item.get("chat_id")
            if not isinstance(chat_id, str) or not chat_id:
                continue
            member_names = self._get_chat_member_names(
                chat_id, self._get_tenant_access_token()
            )
            if user.open_id not in member_names:
                continue
            mode = item.get("chat_mode")
            if mode == "p2p":
                counterparts = [
                    name
                    for member_id, name in member_names.items()
                    if member_id != user.open_id and name.strip()
                ]
                name = "、".join(dict.fromkeys(counterparts)) or "非真人私聊"
                enabled = bool(counterparts) and not self._looks_non_human_private(name)
                conversation_type = ConversationType.PRIVATE
            else:
                raw_name = item.get("name")
                name = (
                    raw_name.strip()
                    if isinstance(raw_name, str) and raw_name.strip()
                    else self.get_chat_name(chat_id)
                )
                enabled = True
                conversation_type = ConversationType.GROUP
            conversations.append(
                Conversation(
                    chat_id=chat_id,
                    name=name,
                    conversation_type=conversation_type,
                    enabled=enabled,
                )
            )
        return conversations

    def _list_chat_records(self) -> list[dict[str, object]]:
        if self._chat_records is not None:
            return self._chat_records
        records: list[dict[str, object]] = []
        page_token = ""
        while True:
            params = {"page_size": "100", "user_id_type": "open_id"}
            if page_token:
                params["page_token"] = page_token
            data = self._openapi_json(
                "GET",
                "/im/v1/chats",
                access_token=self._get_tenant_access_token(),
                params=params,
            )
            items = data.get("items", [])
            if not isinstance(items, list):
                raise FeishuAPIError("chat list API returned invalid items")
            records.extend(item for item in items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            next_token = data.get("page_token")
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token == page_token
            ):
                raise FeishuAPIError("chat list API returned an invalid page_token")
            page_token = next_token
        self._chat_records = records
        for record in records:
            chat_id = record.get("chat_id")
            name = record.get("name")
            if isinstance(chat_id, str) and isinstance(name, str) and name.strip():
                self._chat_names[chat_id] = name.strip()
        return records

    @staticmethod
    def _looks_non_human_private(name: str) -> bool:
        normalized = name.casefold()
        return any(
            marker in normalized
            for marker in ("机器人", "系统助手", "智能助手", "bot", "robot")
        )

    def get_chat_name(self, chat_id: str) -> str:
        if chat_id in self._chat_names:
            return self._chat_names[chat_id]
        data = self._openapi_json(
            "GET",
            f"/im/v1/chats/{quote(chat_id, safe='')}",
            access_token=self._get_tenant_access_token(),
        )
        name = data.get("name")
        resolved = name.strip() if isinstance(name, str) and name.strip() else chat_id
        self._chat_names[chat_id] = resolved
        return resolved

    def list_messages(
        self, chat_id: str, start_time: int, end_time: int
    ) -> list[FeishuMessage]:
        """Read all user-visible messages in one chat and time range."""
        if not chat_id.strip():
            raise ValueError("chat_id must not be empty")
        if start_time > end_time:
            raise ValueError("start_time must not be after end_time")

        messages: list[FeishuMessage] = []
        seen_ids: set[str] = set()
        access_token = self._get_tenant_access_token()
        member_names = self._get_chat_member_names(chat_id, access_token)
        page_token = ""
        while True:
            params = {
                "container_id_type": "chat",
                "container_id": chat_id,
                "start_time": str(start_time),
                "end_time": str(end_time),
                "sort_type": "ByCreateTimeAsc",
                "page_size": "50",
            }
            if page_token:
                params["page_token"] = page_token
            data = self._openapi_json(
                "GET",
                "/im/v1/messages",
                access_token=access_token,
                params=params,
            )
            items = data.get("items", [])
            if not isinstance(items, list):
                raise FeishuAPIError("message API returned invalid items")
            for item in items:
                message = self._parse_message(item, chat_id, member_names)
                if message and message.message_id not in seen_ids:
                    seen_ids.add(message.message_id)
                    messages.append(message)
            if not data.get("has_more"):
                break
            next_token = data.get("page_token")
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token == page_token
            ):
                raise FeishuAPIError("message API returned an invalid page_token")
            page_token = next_token
        return sorted(
            messages, key=lambda message: (message.create_time, message.message_id)
        )

    @classmethod
    def _parse_message(
        cls,
        item: object,
        fallback_chat_id: str,
        member_names: dict[str, str],
    ) -> FeishuMessage | None:
        if not isinstance(item, dict) or item.get("deleted") is True:
            return None
        message_id = item.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return None
        sender = item.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        msg_type = str(item.get("msg_type", ""))
        body = item.get("body")
        raw_content = body.get("content", "") if isinstance(body, dict) else ""
        text = cls._message_text(msg_type, raw_content)
        if not text:
            return None
        try:
            create_time = int(str(item.get("create_time", "0")))
        except ValueError:
            create_time = 0
        return FeishuMessage(
            message_id=message_id,
            chat_id=str(item.get("chat_id") or fallback_chat_id),
            msg_type=msg_type,
            create_time=create_time,
            sender_id=str(sender.get("id", "")),
            sender_type=str(sender.get("sender_type", "")),
            text=text,
            sender_name=member_names.get(str(sender.get("id", "")), ""),
        )

    def _get_chat_member_names(self, chat_id: str, access_token: str) -> dict[str, str]:
        if chat_id in self._chat_member_names:
            return self._chat_member_names[chat_id]
        names: dict[str, str] = {}
        page_token = ""
        while True:
            params = {"member_id_type": "open_id", "page_size": "100"}
            if page_token:
                params["page_token"] = page_token
            data = self._openapi_json(
                "GET",
                f"/im/v1/chats/{quote(chat_id, safe='')}/members",
                access_token=access_token,
                params=params,
            )
            items = data.get("items", [])
            if not isinstance(items, list):
                raise FeishuAPIError("chat member API returned invalid items")
            for item in items:
                if not isinstance(item, dict):
                    continue
                member_id = item.get("member_id")
                name = item.get("name")
                if isinstance(member_id, str) and isinstance(name, str):
                    names[member_id] = name
            if not data.get("has_more"):
                break
            next_token = data.get("page_token")
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token == page_token
            ):
                raise FeishuAPIError("chat member API returned an invalid page_token")
            page_token = next_token
        self._chat_member_names[chat_id] = names
        return names

    @staticmethod
    def _message_text(msg_type: str, raw_content: object) -> str:
        if not isinstance(raw_content, str):
            return ""
        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            content = raw_content
        if msg_type == "text" and isinstance(content, dict):
            return str(content.get("text", "")).strip()
        if msg_type == "post":
            fragments: list[str] = []

            def collect(value: object) -> None:
                if isinstance(value, dict):
                    text = value.get("text")
                    if isinstance(text, str) and text.strip():
                        fragments.append(text.strip())
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(content)
            return " ".join(dict.fromkeys(fragments))
        labels = {
            "image": "[图片]",
            "file": "[文件]",
            "audio": "[音频]",
            "media": "[视频]",
            "sticker": "[表情]",
            "location": "[位置]",
        }
        if msg_type in labels:
            return labels[msg_type]
        if isinstance(content, str):
            return content.strip()
        return ""

    def get_media_download_url(self, minute_token: str) -> str:
        if not minute_token:
            raise ValueError("minute_token must not be empty")
        data = self._openapi_json(
            "GET",
            f"/minutes/v1/minutes/{quote(minute_token, safe='')}/media",
            access_token=self.config.user_access_token,
        )
        download_url = data.get("download_url")
        if not isinstance(download_url, str) or not download_url:
            raise FeishuAPIError("minutes media API returned an empty download_url")
        parsed = urlparse(download_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise FeishuAPIError("minutes media API returned an invalid download_url")
        return download_url

    def download_recording(
        self, recording: Recording, output_dir: Path
    ) -> RecordingFile:
        """Resolve recording.url to a one-day media URL and stream it to disk."""
        download_url = self.get_media_download_url(recording.minute_token)
        try:
            with self._http.stream("GET", download_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                filename = self._recording_filename(
                    response.headers.get("content-disposition", ""),
                    content_type,
                    recording.minute_token,
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
        if size == 0:
            destination.unlink(missing_ok=True)
            raise MediaDownloadError("recording media download returned an empty file")
        return RecordingFile(
            path=destination.resolve(),
            content_type=content_type,
            size_bytes=size,
            source_url=download_url,
        )

    def send_private_text(self, target_open_id: str, text: str) -> str:
        if not text.strip():
            raise ValueError("message text must not be empty")
        return self._send_private_message(target_open_id, "text", {"text": text})

    def send_private_card(
        self,
        target_open_id: str,
        card: dict[str, object],
        idempotency_key: str = "",
    ) -> str:
        if not card:
            raise ValueError("message card must not be empty")
        return self._send_private_message(
            target_open_id,
            "interactive",
            card,
            idempotency_key=idempotency_key,
        )

    def _send_private_message(
        self,
        target_open_id: str,
        msg_type: str,
        content: object,
        idempotency_key: str = "",
    ) -> str:
        if not target_open_id.strip():
            raise ConfigurationError("target user open_id is required to send a DM")
        token = self._get_tenant_access_token()
        params = {"receive_id_type": "open_id"}
        if idempotency_key:
            if len(idempotency_key) > 50:
                raise ValueError(
                    "message idempotency key must not exceed 50 characters"
                )
            params["uuid"] = idempotency_key
        data = self._openapi_json(
            "POST",
            "/im/v1/messages",
            access_token=token,
            params=params,
            json_body={
                "receive_id": target_open_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
            user_open_id=target_open_id,
        )
        message_id = data.get("message_id")
        return str(message_id or "")

    def _get_tenant_access_token(self) -> str:
        if self._tenant_token and time.monotonic() < self._tenant_token_expires_at:
            return self._tenant_token
        if not self.config.app_id or not self.config.app_secret:
            raise ConfigurationError(
                "FEISHU_APP_ID and FEISHU_APP_SECRET are required to send a DM"
            )
        path = "/auth/v3/tenant_access_token/internal"
        try:
            response = self._http.post(
                f"{self.config.base_url}{path}",
                json={
                    "app_id": self.config.app_id,
                    "app_secret": self.config.app_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if self.recorder is not None:
                self.recorder.observe("POST", path, "", False)
            raise FeishuAPIError(
                f"failed to obtain tenant_access_token: {exc}"
            ) from exc
        if payload.get("code") != 0:
            if self.recorder is not None:
                self.recorder.observe("POST", path, "", False)
            raise FeishuAPIError(
                f"failed to obtain tenant_access_token: "
                f"code={payload.get('code')} msg={payload.get('msg', '')}"
            )
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuAPIError("token API returned an empty tenant_access_token")
        if self.recorder is not None:
            self.recorder.observe("POST", path, "", True)
        expires_in = int(payload.get("expire", 7200))
        self._tenant_token = token
        self._tenant_token_expires_at = time.monotonic() + max(expires_in - 60, 0)
        return token

    def _openapi_json(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        user_open_id: str = "",
    ) -> dict[str, object]:
        try:
            response = self._http.request(
                method,
                f"{self.config.base_url}{path}",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            if self.recorder is not None:
                self.recorder.observe(method, path, user_open_id, False, params)
            raise FeishuAPIError(f"Feishu request failed for {path}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as status_exc:
                if self.recorder is not None:
                    self.recorder.observe(method, path, user_open_id, False, params)
                raise FeishuAPIError(
                    f"Feishu request failed for {path}: HTTP {response.status_code}"
                ) from status_exc
            if self.recorder is not None:
                self.recorder.observe(method, path, user_open_id, False, params)
            raise FeishuAPIError(f"Feishu returned invalid JSON for {path}") from exc
        if not isinstance(payload, dict):
            if self.recorder is not None:
                self.recorder.observe(method, path, user_open_id, False, params)
            raise FeishuAPIError(f"Feishu returned a non-object response for {path}")
        if payload.get("code") != 0:
            if self.recorder is not None:
                self.recorder.observe(method, path, user_open_id, False, params)
            raise FeishuAPIError(
                f"Feishu API error for {path}: "
                f"code={payload.get('code')} msg={payload.get('msg', '')}"
            )
        if self.recorder is not None:
            self.recorder.observe(method, path, user_open_id, True, params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise FeishuAPIError(
                f"Feishu request failed for {path}: HTTP {response.status_code}"
            ) from exc
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise FeishuAPIError(f"Feishu returned invalid data for {path}")
        return data

    @staticmethod
    def _recording_filename(
        content_disposition: str, content_type: str, minute_token: str
    ) -> str:
        filename = ""
        if content_disposition:
            message = Message()
            message["content-disposition"] = content_disposition
            filename = message.get_filename() or ""
        filename = Path(filename.replace("\\", "/")).name
        if filename in {"", ".", ".."}:
            preferred = {
                "video/mp4": ".mp4",
                "audio/mp4": ".m4a",
                "audio/mpeg": ".mp3",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/webm": ".webm",
            }
            extension = preferred.get(content_type) or mimetypes.guess_extension(
                content_type
            )
            filename = f"{minute_token}{extension or '.media'}"
        return filename
