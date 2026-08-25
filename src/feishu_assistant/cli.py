from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

from .api_usage import APICallRecorder, usage_range
from .asr_client import ASRClient
from .config import (
    FeishuConfig,
    ProviderConfig,
    app_entry_url_from_env,
    artifact_dir_from_env,
    meeting_summary_enabled_from_env,
    meeting_trigger_initial_lookback_ms_from_env,
    meeting_trigger_max_attempts_from_env,
    meeting_trigger_poll_seconds_from_env,
    meeting_trigger_state_file_from_env,
    message_banner_image_key_from_env,
    message_checkpoint_file_from_env,
    message_digest_enabled_from_env,
    message_digest_times_from_env,
    message_initial_lookback_seconds_from_env,
    message_read_identity_from_env,
    message_retry_attempts_from_env,
    message_timezone_from_env,
    oauth_database_file_from_env,
    oauth_key_file_from_env,
    oauth_redirect_uri_from_env,
    oauth_web_host_from_env,
    oauth_web_port_from_env,
    user_oauth_profile_map_from_env,
    user_open_ids_from_env,
    workspace_state_file_from_env,
)
from .conversation_preferences import ConversationPreferenceStore
from .domain import Conversation, User
from .errors import AssistantError, ConfigurationError
from .feishu_client import FeishuClient
from .llm_client import LLMClient
from .meeting_summary import MeetingSummary
from .meeting_trigger import MeetingTrigger, MeetingTriggerStateStore
from .message_digest import MessageCheckpointStore, MessageDigest
from .oauth import (
    MEETING_OAUTH_SCOPES,
    MESSAGE_DIGEST_OAUTH_SCOPES,
    FeishuOAuthClient,
    OAuthStore,
    OAuthTokenProvider,
    StoredOAuthRunner,
)
from .reliability import retry_call, single_instance_lock
from .runtime_store import RuntimeStore
from .scheduler import DigestScheduler
from .user_identity_client import UserIdentityClient
from .webapp import AssistantWebApp, serve
from .workspace import BitableArchiver, BitableClient, WorkspaceStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feishu-assistant",
        description="Run the Feishu personal message-digest assistant.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    download = subcommands.add_parser(
        "download-recording",
        help="resolve recording.url and download the actual audio/video file",
    )
    download.add_argument("meeting_id")
    download.add_argument(
        "--output-dir",
        type=Path,
        help="destination directory (default: ARTIFACT_DIR/recording-probe)",
    )
    download.add_argument(
        "--target-open-id",
        help="OAuth recording owner; optional when exactly one user is configured",
    )

    run = subcommands.add_parser(
        "run", help="download, transcribe, summarize, and send one meeting"
    )
    run.add_argument("meeting_id")
    run.add_argument(
        "--target-open-id",
        help="meeting-summary recipient; optional when exactly one user is configured",
    )

    digest = subcommands.add_parser(
        "digest", help="read, summarize, and send new messages once"
    )
    digest.add_argument(
        "--end-time",
        type=int,
        help="Unix-second cutoff used for deterministic manual runs",
    )

    subcommands.add_parser(
        "digest-scheduler",
        help="run message digests daily at 08:00, 12:00, and 18:00",
    )

    meeting_trigger = subcommands.add_parser(
        "meeting-trigger",
        help="process new recording links from 智能纪要助手 once",
    )
    meeting_trigger.add_argument(
        "--end-time-ms",
        type=int,
        help="millisecond cutoff used for deterministic manual runs",
    )
    subcommands.add_parser(
        "meeting-trigger-scheduler",
        help="continuously watch 智能纪要助手 for new recordings",
    )
    subcommands.add_parser(
        "oauth-web",
        help="serve /app and /oauth/callback for employee self-service OAuth",
    )
    usage = subcommands.add_parser(
        "api-usage", help="summarize locally observed Feishu HTTP calls"
    )
    usage.add_argument("--day", default="", help="local date, for example 2026-08-19")
    usage.add_argument("--month", default="", help="local month, for example 2026-08")
    usage.add_argument("--user", default="", help="filter by user open_id")
    usage.add_argument("--caller", default="", help="filter by task/caller")
    return parser


def _oauth_store() -> OAuthStore:
    return OAuthStore(oauth_database_file_from_env(), oauth_key_file_from_env())


def _conversation_preference_store() -> ConversationPreferenceStore:
    return ConversationPreferenceStore(oauth_database_file_from_env())


def _runtime_store() -> RuntimeStore:
    return RuntimeStore(oauth_database_file_from_env())


def _api_recorder(caller: str) -> APICallRecorder:
    return APICallRecorder(oauth_database_file_from_env(), caller)


def _oauth_client(
    caller: str = "oauth", store: OAuthStore | None = None
) -> FeishuOAuthClient:
    config = FeishuConfig.for_message_digest()
    token_store = store or _oauth_store()
    return FeishuOAuthClient(
        config.app_id,
        config.app_secret,
        oauth_redirect_uri_from_env(),
        base_url=config.base_url,
        scopes=(
            MESSAGE_DIGEST_OAUTH_SCOPES + MEETING_OAUTH_SCOPES
            if meeting_summary_enabled_from_env()
            else MESSAGE_DIGEST_OAUTH_SCOPES
        ),
        token_store=token_store,
        recorder=_api_recorder(caller),
    )


def _configured_open_ids() -> list[str]:
    stored = [user.open_id for user in _oauth_store().users()]
    return list(dict.fromkeys([*stored, *user_open_ids_from_env()]))


def _target_open_id(args: argparse.Namespace) -> str:
    configured_open_ids = _configured_open_ids()
    target_open_id = args.target_open_id
    if target_open_id is None:
        if len(configured_open_ids) != 1:
            raise ValueError(
                "--target-open-id is required when multiple users are configured"
            )
        return configured_open_ids[0]
    return target_open_id


def _user_identity_client(
    open_id: str, caller: str = "message_digest"
) -> UserIdentityClient:
    store = _oauth_store()
    runtime = _runtime_store()
    if store.get(open_id) is not None:
        oauth_client = _oauth_client(caller, store)
        recorder = _api_recorder(caller)
        runner = StoredOAuthRunner(
            OAuthTokenProvider(store, oauth_client, open_id),
            base_url=oauth_client.base_url,
            recorder=recorder,
            user_open_id=open_id,
        )
        return UserIdentityClient(
            open_id, runner=runner, runtime_store=runtime
        )
    profiles = user_oauth_profile_map_from_env()
    return UserIdentityClient(
        open_id,
        profile=profiles.get(open_id, ""),
        runtime_store=runtime,
    )


def _bitable_client(user: User, caller: str) -> BitableClient:
    profiles = user_oauth_profile_map_from_env()
    oauth_store = _oauth_store()
    if oauth_store.get(user.open_id) is not None:
        oauth_client = _oauth_client(caller, oauth_store)
        runner = StoredOAuthRunner(
            OAuthTokenProvider(oauth_store, oauth_client, user.open_id),
            base_url=oauth_client.base_url,
            recorder=_api_recorder(caller),
            user_open_id=user.open_id,
        )
        return BitableClient(user.open_id, runner=runner)
    return BitableClient(user.open_id, profile=profiles.get(user.open_id, ""))


def _bitable_archiver(caller: str = "archive") -> BitableArchiver:
    """One shared archiver; each user gets their own OAuth bitable client."""
    return BitableArchiver(
        WorkspaceStore(workspace_state_file_from_env()),
        partial(_bitable_client, caller=caller),
        message_timezone_from_env(),
        runtime_store=_runtime_store(),
        include_meeting=meeting_summary_enabled_from_env(),
    )



def _download_recording(args: argparse.Namespace) -> dict[str, object]:
    output_dir = args.output_dir or artifact_dir_from_env() / "recording-probe"
    recordings = _user_identity_client(_target_open_id(args), "download_recording")
    recordings.resolve_user()
    recording = recordings.get_recording(args.meeting_id)
    media = recordings.download_recording(recording, output_dir)
    return {
        "meeting_id": recording.meeting_id,
        "duration": recording.duration,
        "minute_token": recording.minute_token,
        "recording_path": str(media.path),
        "content_type": media.content_type,
        "size_bytes": media.size_bytes,
    }


def _run_pipeline(args: argparse.Namespace) -> dict[str, object]:
    config = FeishuConfig.for_message_digest()
    target_open_id = _target_open_id(args)
    recordings = _user_identity_client(target_open_id, "meeting_summary")
    user = recordings.resolve_user()
    with FeishuClient(config, recorder=_api_recorder("meeting_summary")) as feishu:
        pipeline = MeetingSummary(
            feishu=feishu,
            asr=ASRClient(ProviderConfig.asr_from_env()),
            llm=LLMClient(ProviderConfig.llm_from_env()),
            user=user,
            artifact_dir=artifact_dir_from_env(),
            recording_source=recordings,
            archiver=_bitable_archiver("meeting_summary"),
        )
        result = pipeline.run(args.meeting_id)
    return {
        "meeting_id": result.meeting_id,
        "recording_path": str(result.recording_path),
        "transcript_path": str(result.transcript_path),
        "summary_path": str(result.summary_path),
        "message_id": result.message_id,
        "archive_record_id": result.archive_record_id,
    }


def _configured_meeting_triggers(
    sender: FeishuClient,
    archiver: BitableArchiver,
) -> list[MeetingTrigger]:
    triggers: list[MeetingTrigger] = []
    attempts = message_retry_attempts_from_env()
    stored_users = {user.open_id: user for user in _oauth_store().users()}
    for open_id in _configured_open_ids():
        source = _user_identity_client(open_id, "meeting_trigger")
        user = stored_users.get(open_id)
        if user is None:
            user = retry_call(
                source.resolve_user,
                label="oauth-resolve-user",
                user_open_id=open_id,
                attempts=attempts,
            )
        pipeline = MeetingSummary(
            feishu=sender,
            asr=ASRClient(ProviderConfig.asr_from_env()),
            llm=LLMClient(ProviderConfig.llm_from_env()),
            user=user,
            artifact_dir=artifact_dir_from_env(),
            recording_source=source,
            archiver=archiver,
        )
        triggers.append(
            MeetingTrigger(
                user=user,
                source=source,
                pipeline=pipeline,
                state_store=MeetingTriggerStateStore(
                    meeting_trigger_state_file_from_env(), user.open_id
                ),
                initial_lookback_ms=meeting_trigger_initial_lookback_ms_from_env(),
                max_attempts=meeting_trigger_max_attempts_from_env(),
            )
        )
    return triggers


def _run_meeting_triggers(end_time_ms: int | None = None) -> list[dict[str, object]]:
    attempts = message_retry_attempts_from_env()
    lock_path = artifact_dir_from_env() / "meeting-trigger-scheduler.lock"
    with (
        single_instance_lock(lock_path),
        FeishuClient(
            FeishuConfig.for_message_digest(),
            recorder=_api_recorder("meeting_trigger"),
        ) as sender,
    ):
        archiver = _bitable_archiver("meeting_trigger")
        results = [
            asdict(
                retry_call(
                    partial(trigger.run, end_time_ms),
                    label="meeting-trigger-poll",
                    user_open_id=trigger.user.open_id,
                    attempts=attempts,
                )
            )
            for trigger in _configured_meeting_triggers(sender, archiver)
        ]
    return results


def _run_meeting_trigger_scheduler() -> None:
    interval = meeting_trigger_poll_seconds_from_env()
    attempts = message_retry_attempts_from_env()
    lock_path = artifact_dir_from_env() / "meeting-trigger-scheduler.lock"
    with (
        single_instance_lock(lock_path),
        FeishuClient(
            FeishuConfig.for_message_digest(),
            recorder=_api_recorder("meeting_trigger"),
        ) as sender,
    ):
        active = _configured_meeting_triggers(
            sender, _bitable_archiver("meeting_trigger")
        )
        while active:
            cycle = tuple(active)
            for index, trigger in enumerate(cycle):
                try:
                    result = retry_call(
                        trigger.run,
                        label="meeting-trigger-poll",
                        user_open_id=trigger.user.open_id,
                        attempts=attempts,
                    )
                except AssistantError:
                    logging.getLogger(__name__).exception(
                        "meeting trigger user=%s stage=poll status=disabled",
                        trigger.user.open_id,
                    )
                    active.remove(trigger)
                    continue
                logging.getLogger(__name__).info(
                    "meeting trigger result: %s",
                    json.dumps(asdict(result), ensure_ascii=False),
                )
                if index < len(cycle) - 1:
                    # Keep a large user batch below common per-API burst limits.
                    time.sleep(0.05)
            if active:
                time.sleep(interval)
        raise ConfigurationError("all meeting trigger users are disabled")


def _message_digest(
    reader: UserIdentityClient | FeishuClient,
    sender: FeishuClient,
    user: User,
    conversations: list[Conversation],
    checkpoint_scope: str,
    archiver: BitableArchiver,
) -> MessageDigest:
    return MessageDigest(
        reader=reader,
        sender=sender,
        llm=LLMClient(ProviderConfig.llm_from_env()),
        user=user,
        conversations=conversations,
        checkpoint_store=MessageCheckpointStore(
            message_checkpoint_file_from_env(), scope=checkpoint_scope
        ),
        initial_lookback_seconds=message_initial_lookback_seconds_from_env(),
        timezone=message_timezone_from_env(),
        banner_image_key=message_banner_image_key_from_env(),
        archiver=archiver,
    )


def _configured_digests(
    sender: FeishuClient,
    archiver: BitableArchiver,
    reader_cache: dict[str, UserIdentityClient] | None = None,
) -> list[tuple[User, MessageDigest]]:
    configured: list[tuple[User, MessageDigest]] = []
    identity = message_read_identity_from_env()
    attempts = message_retry_attempts_from_env()
    preferences = _conversation_preference_store()
    oauth_store = _oauth_store()
    oauth_users = {user.open_id: user for user in oauth_store.users()}
    for open_id in _configured_open_ids():
        if identity == "user":
            user_reader = (
                reader_cache.get(open_id) if reader_cache is not None else None
            )
            if user_reader is None:
                user_reader = _user_identity_client(open_id)
                if reader_cache is not None:
                    reader_cache[open_id] = user_reader
            user = oauth_users.get(open_id)
            if user is None:
                user = retry_call(
                    user_reader.resolve_user,
                    label="oauth-resolve-user",
                    user_open_id=open_id,
                    attempts=attempts,
                )
            reader: UserIdentityClient | FeishuClient = user_reader
        else:
            reader = sender
            user = retry_call(
                partial(sender.resolve_user, open_id),
                label="app-resolve-user",
                user_open_id=open_id,
                attempts=attempts,
            )
        if not user.enabled:
            continue
        if identity == "user" and open_id in oauth_users:
            # OAuth users discover conversations when opening the settings page.
            # New conversations are disabled by definition, so scanning every chat
            # again before every scheduled digest cannot change what is consumed.
            conversations = [
                Conversation(
                    chat_id=item.chat_id,
                    name=item.chat_name,
                    conversation_type=item.conversation_type,
                    enabled=True,
                )
                for item in preferences.list_for_user(user.open_id)
                if item.enabled
            ]
        else:
            discovered = retry_call(
                partial(reader.discover_conversations, user),
                label="discover-conversations",
                user_open_id=open_id,
                attempts=attempts,
            )
            if identity == "app":
                discovered = [item for item in discovered if item.enabled]
            discovered = preferences.sync_discovered(user.open_id, discovered)
            conversations = [
                conversation for conversation in discovered if conversation.enabled
            ]
        if isinstance(reader, UserIdentityClient):
            reader.set_conversation_context(user, conversations)
        configured.append(
            (
                user,
                _message_digest(
                    reader,
                    sender,
                    user,
                    conversations,
                    user.open_id if identity == "user" else f"app:{user.open_id}",
                    archiver,
                ),
            )
        )
    return configured


def _run_digest(args: argparse.Namespace) -> dict[str, object]:
    end_time = args.end_time if args.end_time is not None else int(time.time())
    config = FeishuConfig.for_message_digest()
    attempts = message_retry_attempts_from_env()
    lock_path = artifact_dir_from_env() / "digest-scheduler.lock"
    with single_instance_lock(lock_path), FeishuClient(
        config, recorder=_api_recorder("message_digest")
    ) as feishu:
        archiver = _bitable_archiver("message_digest")
        results = [
            (
                user,
                retry_call(
                    partial(digest.run, end_time),
                    label=f"message-digest-{end_time}",
                    user_open_id=user.open_id,
                    attempts=attempts,
                ),
            )
            for user, digest in _configured_digests(feishu, archiver)
        ]
    if len(results) == 1:
        return asdict(results[0][1])
    return {
        "users": [
            {
                "open_id": user.open_id,
                "name": user.name,
                "outcome": asdict(outcome),
            }
            for user, outcome in results
        ]
    }


def _run_digest_scheduler() -> None:
    config = FeishuConfig.for_message_digest()
    attempts = message_retry_attempts_from_env()
    lock_path = artifact_dir_from_env() / "digest-scheduler.lock"
    with single_instance_lock(lock_path), FeishuClient(
        config, recorder=_api_recorder("message_digest")
    ) as feishu:
        archiver = _bitable_archiver("message_digest")
        reader_cache: dict[str, UserIdentityClient] = {}

        def run(end_time: int) -> object:
            outcomes = []
            for user, digest in _configured_digests(
                feishu, archiver, reader_cache=reader_cache
            ):
                outcome = retry_call(
                    partial(digest.run, end_time),
                    label=f"message-digest-{end_time}",
                    user_open_id=user.open_id,
                    attempts=attempts,
                )
                outcomes.append(outcome)
                logging.getLogger(__name__).info(
                    "message digest result for %s: %s",
                    user.name,
                    json.dumps(asdict(outcome), ensure_ascii=False),
                )
            return outcomes

        DigestScheduler(
            run,
            message_timezone_from_env(),
            times=message_digest_times_from_env(),
        ).run_forever()


def _run_oauth_web() -> None:
    redirect_uri = oauth_redirect_uri_from_env()
    oauth_store = _oauth_store()
    with FeishuClient(
        FeishuConfig.for_message_digest(), recorder=_api_recorder("oauth_web")
    ) as sender:
        app = AssistantWebApp(
            oauth_store,
            _oauth_client("oauth_web", oauth_store),
            WorkspaceStore(workspace_state_file_from_env()),
            runtime_store=_runtime_store(),
            secure_cookie=urlparse(redirect_uri).scheme == "https",
            card_sender=sender,
            app_url=app_entry_url_from_env(),
            include_meeting=meeting_summary_enabled_from_env(),
        )
        serve(app, oauth_web_host_from_env(), oauth_web_port_from_env())


def _run_api_usage(args: argparse.Namespace) -> dict[str, object]:
    start, end = usage_range(
        day=args.day,
        month=args.month,
        timezone=message_timezone_from_env(),
    )
    summary = _api_recorder("api_usage").summarize(
        start_timestamp=start,
        end_timestamp=end,
        user_open_id=args.user,
        caller=args.caller,
    )
    return asdict(summary)


def _enforce_feature_flags(args: argparse.Namespace) -> None:
    meeting_command = args.command in {
        "download-recording",
        "run",
        "meeting-trigger",
        "meeting-trigger-scheduler",
    }
    if meeting_command and not meeting_summary_enabled_from_env():
        raise ConfigurationError(
            "meeting summary is disabled by features.meeting_summary=false"
        )
    digest_command = args.command in {"digest", "digest-scheduler"}
    if digest_command and not message_digest_enabled_from_env():
        raise ConfigurationError(
            "message digest is disabled by features.message_digest=false"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    result: object
    try:
        _enforce_feature_flags(args)
        if args.command == "download-recording":
            result = _download_recording(args)
        elif args.command == "run":
            result = _run_pipeline(args)
        elif args.command == "digest":
            result = _run_digest(args)
        elif args.command == "meeting-trigger":
            result = _run_meeting_triggers(args.end_time_ms)
        elif args.command == "meeting-trigger-scheduler":
            _run_meeting_trigger_scheduler()
            return 0
        elif args.command == "oauth-web":
            _run_oauth_web()
            return 0
        elif args.command == "api-usage":
            result = _run_api_usage(args)
        else:
            _run_digest_scheduler()
            return 0
    except (AssistantError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
