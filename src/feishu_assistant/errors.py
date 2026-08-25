class AssistantError(Exception):
    """Base error shown by the CLI without a traceback."""


class ConfigurationError(AssistantError):
    """Required runtime configuration is missing or invalid."""


class FeishuAPIError(AssistantError):
    """A Feishu OpenAPI request failed or returned invalid data."""


class MediaDownloadError(AssistantError):
    """The recording media could not be downloaded."""


class ASRError(AssistantError):
    """The ASR provider request failed."""


class NoEffectiveSpeechError(ASRError):
    """ASR completed successfully but found no usable speech."""


class LLMError(AssistantError):
    """The LLM provider failed or returned no summary."""


class CheckpointError(AssistantError):
    """The message checkpoint could not be read or written."""
