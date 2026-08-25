from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import OpenAI

from .config import ProviderConfig
from .errors import ASRError, NoEffectiveSpeechError


class ASRClient:
    """Audio transcription adapter for OpenAI and DashScope filetrans APIs."""

    def __init__(
        self,
        config: ProviderConfig,
        client: Any | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._client = client
        self._http = http
        self._sleep = sleep

    def transcribe(self, recording: Path, source_url: str = "") -> str:
        if not recording.is_file():
            raise ASRError(f"recording file does not exist: {recording}")
        if self._uses_dashscope_filetrans():
            return self._transcribe_dashscope_file(source_url)
        return self._transcribe_openai(recording)

    def _uses_dashscope_filetrans(self) -> bool:
        return (
            self.config.model.casefold().endswith("filetrans")
            and "maas.aliyuncs.com" in self.config.base_url.casefold()
        )

    def _transcribe_openai(self, recording: Path) -> str:
        client = self._client or OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=1800.0,
        )
        try:
            with recording.open("rb") as audio:
                result = client.audio.transcriptions.create(
                    model=self.config.model,
                    file=audio,
                    response_format="json",
                )
        except Exception as exc:
            raise ASRError(f"ASR transcription failed: {exc}") from exc
        text = getattr(result, "text", None)
        if text is None and isinstance(result, dict):
            text = result.get("text")
        return self._require_text(text)

    def _transcribe_dashscope_file(self, source_url: str) -> str:
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ASRError(
                "DashScope filetrans requires the recording's temporary public URL"
            )
        api_base = self._dashscope_api_base()
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        client = self._http or httpx.Client(
            timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True
        )
        try:
            response = client.post(
                f"{api_base}/services/audio/asr/transcription",
                headers=headers,
                json={
                    "model": self.config.model,
                    "input": {"file_urls": [source_url]},
                    "parameters": {
                        "channel_id": [0],
                        "language_hints": ["zh", "en"],
                    },
                },
            )
            response.raise_for_status()
            payload = self._json_object(response, "task submission")
            output = payload.get("output")
            task_id = output.get("task_id") if isinstance(output, dict) else None
            if not isinstance(task_id, str) or not task_id:
                raise ASRError("DashScope ASR returned no task_id")
            result_urls = self._wait_for_dashscope_task(
                client, api_base, headers, task_id
            )
            texts: list[str] = []
            for result_url in result_urls:
                result_response = client.get(result_url)
                result_response.raise_for_status()
                result = self._json_object(result_response, "transcription result")
                transcripts = result.get("transcripts", [])
                if not isinstance(transcripts, list):
                    continue
                for transcript in transcripts:
                    text = (
                        transcript.get("text") if isinstance(transcript, dict) else None
                    )
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())
        except httpx.HTTPError as exc:
            raise ASRError(f"DashScope ASR request failed: {exc}") from exc
        finally:
            if self._http is None:
                client.close()
        return self._require_text("\n".join(texts))

    def _wait_for_dashscope_task(
        self,
        client: httpx.Client,
        api_base: str,
        headers: dict[str, str],
        task_id: str,
    ) -> list[str]:
        query_headers = {"Authorization": headers["Authorization"]}
        for _ in range(1800):
            response = client.get(f"{api_base}/tasks/{task_id}", headers=query_headers)
            response.raise_for_status()
            payload = self._json_object(response, "task query")
            output = payload.get("output")
            if not isinstance(output, dict):
                raise ASRError("DashScope ASR returned invalid task output")
            status = str(output.get("task_status", "")).upper()
            if status == "SUCCEEDED":
                results = output.get("results", [])
                if not isinstance(results, list):
                    raise ASRError("DashScope ASR returned invalid task results")
                urls: list[str] = []
                for result in results:
                    if not isinstance(result, dict):
                        continue
                    result_url = result.get("transcription_url")
                    if result.get("subtask_status") == "SUCCEEDED" and isinstance(
                        result_url, str
                    ):
                        urls.append(result_url)
                if not urls:
                    raise ASRError("DashScope ASR task returned no successful result")
                return urls
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                message = output.get("message", "")
                raise ASRError(f"DashScope ASR task {status.lower()}: {message}")
            self._sleep(1.0)
        raise ASRError("DashScope ASR task timed out")

    def _dashscope_api_base(self) -> str:
        base = self.config.base_url.rstrip("/")
        suffix = "/compatible-mode/v1"
        if base.endswith(suffix):
            return f"{base.removesuffix(suffix)}/api/v1"
        return base

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ASRError(
                f"DashScope ASR returned invalid JSON for {operation}"
            ) from exc
        if not isinstance(payload, dict):
            raise ASRError(f"DashScope ASR returned invalid data for {operation}")
        return payload

    @staticmethod
    def _require_text(text: object) -> str:
        if not isinstance(text, str) or not text.strip():
            raise NoEffectiveSpeechError(
                "ASR completed but returned no effective speech"
            )
        return text.strip()
