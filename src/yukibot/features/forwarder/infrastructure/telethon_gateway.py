"""Telethon v2 implementation of the forwarder's Telegram port."""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
from typing import cast

from yukibot.adapters.telegram.client import (
    NativeClient,
    NativeMessage,
    NativePeer,
    PeerRegistry,
    peer_dialog_id,
)
from yukibot.adapters.telegram.rate_limit import TelegramRequestLimiter

from ..errors import (
    MessageNotFound,
    MessageNotModified,
    NativeForwardUnsupported,
    PermanentDeliveryError,
    RetryAfter,
)
from ..models import ContentType, DestinationEndpoint, ForwardMode, IncomingMessage, MessageRef


class TelethonGateway:
    def __init__(
        self,
        client: NativeClient,
        peers: PeerRegistry,
        *,
        request_limiter: TelegramRequestLimiter | None = None,
        max_concurrency: int = 4,
        messages_per_second: int = 20,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if messages_per_second <= 0:
            raise ValueError("messages_per_second must be positive")
        self._client = client
        self._peers = peers
        self._request_limiter = request_limiter or TelegramRequestLimiter(
            max_concurrency=max_concurrency,
            messages_per_second=messages_per_second,
        )

    async def deliver_message(
        self,
        message: IncomingMessage,
        destination: DestinationEndpoint,
        *,
        mode: ForwardMode,
        reply_to_message_id: int | None,
    ) -> MessageRef:
        async with self._request_limiter.slot(destination.chat_id):
            try:
                native = await self._get_message(message.ref)
                target = self._peer(destination.chat_id)
                if mode is ForwardMode.FORWARD:
                    sent = await self._forward((native,), target, destination, reply_to_message_id)
                    return _message_ref(sent[0])
                result = await self._copy_one(
                    native,
                    target,
                    destination,
                    reply_to_message_id,
                )
                return _message_ref(result)
            except Exception as error:
                raise _translate_error(error, native_forward=mode is ForwardMode.FORWARD) from error

    async def deliver_album(
        self,
        messages: Sequence[IncomingMessage],
        destination: DestinationEndpoint,
        *,
        mode: ForwardMode,
        reply_to_message_id: int | None,
    ) -> Sequence[MessageRef]:
        async with self._request_limiter.slot(destination.chat_id):
            try:
                native = tuple([await self._get_message(message.ref) for message in messages])
                target = self._peer(destination.chat_id)
                if mode is ForwardMode.FORWARD:
                    sent = await self._forward(native, target, destination, reply_to_message_id)
                else:
                    sent = await self._copy_album(
                        tuple(zip(messages, native, strict=True)),
                        target,
                        destination,
                        reply_to_message_id,
                    )
                return tuple(_message_ref(message) for message in sent)
            except Exception as error:
                raise _translate_error(error, native_forward=mode is ForwardMode.FORWARD) from error

    async def send_text(
        self,
        text: str,
        destination: DestinationEndpoint,
        *,
        reply_to_message_id: int | None,
    ) -> MessageRef:
        async with self._request_limiter.slot(destination.chat_id):
            try:
                result = await self._client.send_message(
                    self._peer(destination.chat_id),
                    text,
                    reply_to=_effective_reply(destination, reply_to_message_id),
                )
                return _message_ref(result)
            except Exception as error:
                raise _translate_error(error) from error

    async def edit_from_source(self, source: IncomingMessage, target: MessageRef) -> None:
        async with self._request_limiter.slot(target.chat_id):
            try:
                native_source = await self._get_message(source.ref)
                html = native_source.text_html
                await self._client.edit_message(
                    self._peer(target.chat_id),
                    target.message_id,
                    html=html if html else None,
                    text=None if html else (source.text or source.caption),
                )
            except Exception as error:
                raise _translate_error(error) from error

    async def delete_message(self, target: MessageRef) -> None:
        async with self._request_limiter.slot(target.chat_id):
            try:
                await self._client.delete_messages(
                    self._peer(target.chat_id), [target.message_id], revoke=True
                )
            except Exception as error:
                raise _translate_error(error) from error

    async def _get_message(self, source: MessageRef) -> NativeMessage:
        peer = self._peer(source.chat_id)
        messages = await self._client.get_messages_with_ids(peer, [source.message_id])
        if not messages or not messages[0] or messages[0].id <= 0:
            raise MessageNotFound(f"message {source.chat_id}/{source.message_id} not found")
        return messages[0]

    async def _forward(
        self,
        messages: Sequence[NativeMessage],
        target: NativePeer,
        destination: DestinationEndpoint,
        reply_to_message_id: int | None,
    ) -> Sequence[NativeMessage]:
        if destination.topic_id is not None or reply_to_message_id is not None:
            raise NativeForwardUnsupported("Telethon v2 native forward has no topic/reply option")
        if any(not message.can_forward for message in messages):
            raise NativeForwardUnsupported("source content has forwarding protection")
        source = messages[0].chat
        if any(peer_dialog_id(message.chat) != peer_dialog_id(source) for message in messages):
            raise ValueError("native forwarded messages must share a source chat")
        return await self._client.forward_messages(
            target, [message.id for message in messages], source
        )

    async def _copy_one(
        self,
        source: NativeMessage,
        target: NativePeer,
        destination: DestinationEndpoint,
        reply_to_message_id: int | None,
    ) -> NativeMessage:
        reply_to = _effective_reply(destination, reply_to_message_id)
        if source.file is not None:
            if source.photo is not None:
                return await self._client.send_photo(
                    target,
                    source.file,
                    caption_html=source.text_html,
                    reply_to=reply_to,
                )
            return await self._client.send_file(
                target,
                source.file,
                caption_html=source.text_html,
                reply_to=reply_to,
            )
        if source.text is not None:
            return await self._client.send_message(
                target,
                html=source.text_html if source.text_html else None,
                text=None if source.text_html else source.text,
                reply_to=reply_to,
            )
        raise PermanentDeliveryError("this Telegram media type cannot be copied")

    async def _copy_album(
        self,
        messages: tuple[tuple[IncomingMessage, NativeMessage], ...],
        target: NativePeer,
        destination: DestinationEndpoint,
        reply_to_message_id: int | None,
    ) -> Sequence[NativeMessage]:
        album = self._client.prepare_album()
        for domain, native in messages:
            if native.file is None:
                raise PermanentDeliveryError("album item has no downloadable file")
            data = BytesIO()
            await self._client.download(native.file, data)
            data.seek(0)
            if domain.content_type is ContentType.PHOTO:
                await album.add_photo(data, caption_html=native.text_html)
            elif domain.content_type in (ContentType.VIDEO, ContentType.ANIMATION):
                await album.add_video(data, caption_html=native.text_html)
            else:
                raise PermanentDeliveryError(
                    f"{domain.content_type.value} cannot be copied as an album item"
                )
        return await album.send(target, reply_to=_effective_reply(destination, reply_to_message_id))

    def _peer(self, chat_id: int) -> NativePeer:
        peer = self._peers.get(chat_id)
        if peer is None:
            raise PermanentDeliveryError(
                f"chat {chat_id} is not in the Telethon peer cache; open or join it first"
            )
        return peer


def _effective_reply(
    destination: DestinationEndpoint, reply_to_message_id: int | None
) -> int | None:
    return reply_to_message_id if reply_to_message_id is not None else destination.topic_id


def _message_ref(message: NativeMessage) -> MessageRef:
    if message.id <= 0:
        raise MessageNotFound("Telegram did not return a sent message")
    return MessageRef(peer_dialog_id(message.chat), message.id)


def _translate_error(error: Exception, *, native_forward: bool = False) -> Exception:
    if isinstance(
        error,
        (
            MessageNotFound,
            MessageNotModified,
            NativeForwardUnsupported,
            PermanentDeliveryError,
            RetryAfter,
        ),
    ):
        return error
    name = cast(str | None, getattr(error, "name", None))
    value = cast(int | None, getattr(error, "value", None))
    if name is not None:
        if name.startswith("FLOOD_WAIT") or name.startswith("SLOWMODE_WAIT"):
            return RetryAfter(float(value or 1))
        if name in {"MESSAGE_NOT_MODIFIED"}:
            return MessageNotModified(str(error))
        if name in {"MESSAGE_ID_INVALID", "MSG_ID_INVALID"}:
            return MessageNotFound(str(error))
        if native_forward and name in {
            "CHAT_FORWARDS_RESTRICTED",
            "CHAT_SEND_MEDIA_FORBIDDEN",
            "USER_BANNED_IN_CHANNEL",
        }:
            return NativeForwardUnsupported(str(error))
        return PermanentDeliveryError(f"Telegram RPC {name}: {error}")
    if isinstance(error, (OSError, TimeoutError, ConnectionError)):
        return RetryAfter(1)
    return error
