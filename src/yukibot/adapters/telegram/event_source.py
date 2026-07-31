"""Translate Telethon v2 updates into stable application events."""

from __future__ import annotations

from collections.abc import Callable
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
from yukibot.kernel import EventBus

from .client import (
    NativeClient,
    NativeDeletedEvent,
    NativeHandler,
    NativeMessage,
    PeerRegistry,
    peer_dialog_id,
    telethon_event_types,
)


class TelethonEventSource:
    name = "telegram"

    def __init__(
        self,
        client: NativeClient,
        bus: EventBus,
        peers: PeerRegistry,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._bus = bus
        self._peers = peers
        self._now = now or (lambda: datetime.now(UTC))
        self._event_types: tuple[type[object], type[object], type[object]] | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        event_types = telethon_event_types()
        handlers: tuple[NativeHandler, NativeHandler, NativeHandler] = (
            self._handle_new,
            self._handle_edit,
            self._handle_delete,
        )
        for handler, event_type in zip(handlers, event_types, strict=True):
            self._client.add_event_handler(handler, event_type)
        self._event_types = event_types
        try:
            await self._client.connect()
            if not await self._client.is_authorized():
                await self._client.interactive_login()
            await self.refresh_peers()
        except BaseException:
            self._remove_handlers()
            await self._client.disconnect()
            raise
        self._started = True

    async def stop(self) -> None:
        if not self._started and self._event_types is None:
            return
        self._remove_handlers()
        self._started = False

    async def refresh_peers(self) -> None:
        dialogs = await self._client.get_dialogs()
        for dialog in dialogs:
            self._peers.remember(dialog.chat)

    async def _handle_new(self, raw_event: object) -> object:
        event = cast(NativeMessage, raw_event)
        self._remember_message_peers(event)
        await self._bus.publish(TelegramMessageReceived(normalize_message(event, self._now())))
        return None

    async def _handle_edit(self, raw_event: object) -> object:
        event = cast(NativeMessage, raw_event)
        self._remember_message_peers(event)
        await self._bus.publish(TelegramMessageEdited(normalize_message(event, self._now())))
        return None

    async def _handle_delete(self, raw_event: object) -> object:
        event = cast(NativeDeletedEvent, raw_event)
        chat_id = _channel_dialog_id(event.channel_id) if event.channel_id is not None else None
        await self._bus.publish(
            TelegramMessagesDeleted(tuple(event.message_ids), self._now(), chat_id=chat_id)
        )
        return None

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
    return TelegramMessage(
        ref=MessageRef(peer_dialog_id(message.chat), message.id),
        content_type=content_type,
        occurred_at=occurred_at,
        sender_id=peer_dialog_id(message.sender) if message.sender is not None else None,
        topic_id=_topic_id(raw, message.id),
        grouped_id=message.grouped_id,
        text=None if is_caption else raw_text,
        caption=raw_text if is_caption else None,
        reply_to_message_id=message.replied_message_id,
        service=service,
        outgoing=message.outgoing,
    )


def _content_type(message: NativeMessage) -> TelegramContentType:
    raw_name = type(message._raw).__name__
    if raw_name == "MessageService":
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
