"""Translate Telethon updates into stable application events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from yukibot.contracts import (
    MessageRef,
    TelegramContentType,
    TelegramMessage,
    TelegramMessageEdited,
    TelegramMessageReceived,
    TelegramMessagesDeleted,
    TelegramServiceKind,
    TelegramServiceMessage,
)
from yukibot.kernel import EventBus, TaskSupervisor

from .client import (
    NativeClient,
    NativeDeletedEvent,
    NativeHandler,
    NativeMessage,
    PeerRegistry,
    telethon_event_types,
)
from .commands import IncomingCommandRouter


class TelethonEventSource:
    name = "telegram"

    def __init__(
        self,
        client: NativeClient,
        bus: EventBus,
        peers: PeerRegistry,
        *,
        supervisor: TaskSupervisor,
        commands: IncomingCommandRouter | None = None,
        now: Callable[[], datetime] | None = None,
        drain_timeout: float = 15.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if drain_timeout <= 0:
            raise ValueError("drain_timeout must be positive")
        self._client = client
        self._bus = bus
        self._peers = peers
        self._supervisor = supervisor
        self._commands = commands
        self._now = now or (lambda: datetime.now(UTC))
        self._drain_timeout = drain_timeout
        self._logger = logger or logging.getLogger(__name__)
        self._event_types: tuple[type[object], type[object], type[object]] | None = None
        self._started = False
        self._accepting = False
        self._inflight: set[asyncio.Task[object]] = set()
        self._update_pump: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._started:
            return
        event_types = telethon_event_types()
        handlers: tuple[NativeHandler, NativeHandler, NativeHandler] = (
            self._handle_new,
            self._handle_edit,
            self._handle_delete,
        )
        self._accepting = True
        self._event_types = event_types
        try:
            for handler, event_type in zip(handlers, event_types, strict=True):
                self._client.add_event_handler(handler, event_type)
            # Stable Telethon owns its receive loop; this task monitors the
            # connection and turns an unexpected disconnect into a critical failure.
            self._update_pump = self._supervisor.create_task(
                self._run_update_pump(),
                name="telegram:update-pump",
                critical=True,
            )
        except BaseException:
            self._accepting = False
            self._remove_handlers()
            raise
        self._started = True

    async def stop(self) -> None:
        if not self._started and self._event_types is None and self._update_pump is None:
            return
        self._accepting = False
        self._remove_handlers()
        self._started = False
        pump, self._update_pump = self._update_pump, None
        if pump is not None:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        inflight = tuple(self._inflight)
        if not inflight:
            return
        _, pending = await asyncio.wait(inflight, timeout=self._drain_timeout)
        if not pending:
            return
        self._logger.error(
            "telegram event handlers did not drain before timeout",
            extra={"task_count": len(pending), "timeout": self._drain_timeout},
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _handle_new(self, raw_event: object) -> object:
        if not self._accepting:
            return None
        task = self._track_current_task()
        try:
            event = cast(NativeMessage, raw_event)
            self._remember_message_peers(event)
            message = normalize_message(event, self._now())
            self._logger.info(
                "telegram message received",
                extra={
                    "chat_id": message.ref.chat_id,
                    "message_id": message.ref.message_id,
                    "sender_id": message.sender_id,
                    "outgoing": message.outgoing,
                    "content_type": message.content_type.value,
                },
            )
            if self._commands is not None and await self._commands.route(message):
                return None
            await self._bus.publish(TelegramMessageReceived(message))
        finally:
            self._release_task(task)
        return None

    async def _run_update_pump(self) -> None:
        await self._client.run_until_disconnected()
        if self._accepting:
            raise RuntimeError("Telegram update pump stopped while the event source was active")

    async def _handle_edit(self, raw_event: object) -> object:
        if not self._accepting:
            return None
        task = self._track_current_task()
        try:
            event = cast(NativeMessage, raw_event)
            self._remember_message_peers(event)
            observed_at = self._now()
            message = normalize_message(event, observed_at)
            if message.edited_at is None:
                message = replace(message, edited_at=observed_at)
            if self._commands is not None and await self._commands.route(message, execute=False):
                return None
            await self._bus.publish(TelegramMessageEdited(message))
        finally:
            self._release_task(task)
        return None

    async def _handle_delete(self, raw_event: object) -> object:
        if not self._accepting:
            return None
        task = self._track_current_task()
        try:
            event = cast(NativeDeletedEvent, raw_event)
            chat_id = event.chat_id
            if chat_id is None and event.channel_id is not None:
                chat_id = _channel_dialog_id(event.channel_id)
            await self._bus.publish(
                TelegramMessagesDeleted(tuple(event.message_ids), self._now(), chat_id=chat_id)
            )
        finally:
            self._release_task(task)
        return None

    def _track_current_task(self) -> asyncio.Task[object] | None:
        task = asyncio.current_task()
        if task is None:
            return None
        tracked = cast(asyncio.Task[object], task)
        self._inflight.add(tracked)
        return tracked

    def _release_task(self, task: asyncio.Task[object] | None) -> None:
        if task is not None:
            self._inflight.discard(task)

    def _remember_message_peers(self, event: NativeMessage) -> None:
        self._peers.remember(event.chat)
        self._peers.remember(event.sender)

    def _remove_handlers(self) -> None:
        if self._event_types is None:
            return
        self._client.remove_event_handler(self._handle_new)
        self._client.remove_event_handler(self._handle_edit)
        self._client.remove_event_handler(self._handle_delete)
        self._event_types = None


def normalize_message(message: NativeMessage, fallback_date: datetime) -> TelegramMessage:
    content_type = _content_type(message)
    raw = message._raw
    service = _service_message(raw) if content_type is TelegramContentType.SERVICE else None
    raw_text = message.text
    is_caption = content_type not in (TelegramContentType.TEXT, TelegramContentType.SERVICE)
    occurred_at = message.date if isinstance(message.date, datetime) else fallback_date
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    edited_at = _normalized_date(getattr(raw, "edit_date", None))
    return TelegramMessage(
        ref=MessageRef(message.chat_id, message.id),
        content_type=content_type,
        occurred_at=occurred_at,
        sender_id=message.sender_id,
        topic_id=_topic_id(raw, message.id),
        grouped_id=message.grouped_id,
        text=None if is_caption else raw_text,
        caption=raw_text if is_caption else None,
        reply_to_message_id=message.replied_message_id,
        service=service,
        outgoing=message.outgoing,
        edited_at=edited_at,
    )


def _content_type(message: NativeMessage) -> TelegramContentType:
    raw_name = type(message._raw).__name__
    if raw_name == "MessageService" or getattr(message._raw, "action", None) is not None:
        return TelegramContentType.SERVICE
    media = getattr(message._raw, "media", None)
    media_name = type(media).__name__
    if media_name in ("NoneType", "MessageMediaEmpty", "MessageMediaWebPage"):
        return TelegramContentType.TEXT
    direct_media = {
        "MessageMediaPoll": TelegramContentType.POLL,
        "MessageMediaGeo": TelegramContentType.LOCATION,
        "MessageMediaContact": TelegramContentType.CONTACT,
        "MessageMediaVenue": TelegramContentType.VENUE,
        "MessageMediaDice": TelegramContentType.DICE,
        "MessageMediaGame": TelegramContentType.GAME,
        "MessageMediaPhoto": TelegramContentType.PHOTO,
    }
    if media_name in direct_media:
        return direct_media[media_name]
    if media_name == "MessageMediaDocument":
        attribute_names = {
            type(attribute).__name__ for attribute in getattr(message.file, "_attributes", ())
        }
        if "DocumentAttributeSticker" in attribute_names:
            return TelegramContentType.STICKER
        if "DocumentAttributeAnimated" in attribute_names:
            return TelegramContentType.ANIMATION
        for attribute in getattr(message.file, "_attributes", ()):
            if type(attribute).__name__ == "DocumentAttributeAudio":
                return (
                    TelegramContentType.VOICE
                    if bool(getattr(attribute, "voice", False))
                    else TelegramContentType.AUDIO
                )
            if type(attribute).__name__ == "DocumentAttributeVideo":
                return (
                    TelegramContentType.VIDEO_NOTE
                    if bool(getattr(attribute, "round_message", False))
                    else TelegramContentType.VIDEO
                )
        return TelegramContentType.DOCUMENT
    return TelegramContentType.OTHER


def _normalized_date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    return None


def _topic_id(raw: object, message_id: int) -> int | None:
    reply = getattr(raw, "reply_to", None)
    if reply is None or not bool(getattr(reply, "forum_topic", False)):
        action_name = type(getattr(raw, "action", None)).__name__
        return message_id if action_name == "MessageActionTopicCreate" else None
    return (
        cast(int | None, getattr(reply, "reply_to_top_id", None))
        or cast(int | None, getattr(reply, "reply_to_msg_id", None))
        or 1
    )


def _service_message(raw: object) -> TelegramServiceMessage:
    action = getattr(raw, "action", None)
    name = type(action).__name__
    kind = {
        "MessageActionChatAddUser": TelegramServiceKind.MEMBERS_JOINED,
        "MessageActionChatJoinedByLink": TelegramServiceKind.MEMBERS_JOINED,
        "MessageActionChatJoinedByRequest": TelegramServiceKind.MEMBERS_JOINED,
        "MessageActionChatDeleteUser": TelegramServiceKind.MEMBER_LEFT,
        "MessageActionPinMessage": TelegramServiceKind.MESSAGE_PINNED,
        "MessageActionChatEditTitle": TelegramServiceKind.TITLE_CHANGED,
        "MessageActionTopicCreate": TelegramServiceKind.TOPIC_CREATED,
    }.get(name, TelegramServiceKind.OTHER)
    if name == "MessageActionTopicEdit":
        closed = getattr(action, "closed", None)
        if closed is True:
            kind = TelegramServiceKind.TOPIC_CLOSED
        elif closed is False:
            kind = TelegramServiceKind.TOPIC_REOPENED
    title = getattr(action, "title", None)
    return TelegramServiceMessage(
        kind,
        new_title=title if isinstance(title, str) else None,
    )


def _channel_dialog_id(channel_id: int) -> int:
    return -(1_000_000_000_000 + channel_id)
