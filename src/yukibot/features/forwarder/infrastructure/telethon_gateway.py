"""Telethon implementation of the forwarder's Telegram port."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import cast
from urllib.parse import parse_qs, urlparse

from yukibot.adapters.telegram.client import (
    NativeClient,
    NativeMessage,
    NativePeer,
    PeerRegistry,
    peer_dialog_id,
)
from yukibot.adapters.telegram.event_source import normalize_message
from yukibot.adapters.telegram.rate_limit import TelegramRequestLimiter

from ..errors import (
    MessageNotFound,
    MessageNotModified,
    NativeForwardUnsupported,
    PermanentDeliveryError,
    RetryAfter,
)
from ..models import (
    ChatIdentity,
    ContentType,
    DestinationEndpoint,
    ForwardMode,
    IncomingMessage,
    MessageRef,
    SourceEndpoint,
)
from ..recovery import ChatAccess, ChatInspection, RebuildJoinResult


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

    async def resolve_chat(self, reference: str) -> ChatIdentity:
        normalized_reference = reference.strip()
        invite_hash = _invite_hash(normalized_reference)
        if invite_hash is not None:
            try:
                peer = await self._client.check_chat_invite(invite_hash)
                if peer is None:
                    joined = tuple(await self._client.join_chat_invite(invite_hash))
                    if len(joined) != 1:
                        raise ValueError("Telegram invite did not return exactly one chat")
                    peer = joined[0]
                self._peers.remember(peer)
                return ChatIdentity(
                    peer_dialog_id(peer),
                    _chat_username(peer),
                    normalized_reference,
                )
            except Exception as error:
                if type(error).__name__ == "InviteRequestSentError":
                    raise PermanentDeliveryError(
                        "入群申请已提交, 审批通过后请重新执行路由命令"
                    ) from error
                raise _translate_error(error) from error

        native_reference: int | str
        try:
            native_reference = int(normalized_reference)
        except ValueError:
            username = _public_username(normalized_reference)
            if username is None:
                raise ValueError("频道引用必须是 ID、@用户名或 Telegram 邀请链接") from None
            native_reference = f"@{username}"
        try:
            peer = self._peers.get(native_reference) if isinstance(native_reference, int) else None
            if peer is None:
                peer = await self._client.resolve_peer(native_reference)
                self._peers.remember(peer)
            resolved_username = getattr(peer, "username", None)
            return ChatIdentity(
                peer_dialog_id(peer),
                resolved_username
                if isinstance(resolved_username, str) and resolved_username
                else None,
            )
        except Exception as error:
            raise _translate_error(error) from error

    async def ensure_source(self, source: SourceEndpoint, *, join: bool) -> None:
        try:
            peer = await self._source_peer(source)
            if join:
                peer = await self._client.join_channel(peer)
                self._peers.remember(peer)
        except Exception as error:
            raise _translate_error(error) from error

    async def inspect_chats(self, chat_ids: Sequence[int]) -> Sequence[ChatInspection]:
        try:
            dialogs = await self._client.get_dialogs()
        except Exception as error:
            raise _translate_error(error) from error
        joined: dict[int, NativePeer] = {}
        for dialog in dialogs:
            peer = dialog.chat
            chat_id = peer_dialog_id(peer)
            joined[chat_id] = peer
            self._peers.remember(peer)

        inspections: list[ChatInspection] = []
        for chat_id in chat_ids:
            inspected_peer = joined.get(chat_id)
            if inspected_peer is None:
                inspections.append(ChatInspection(ChatAccess(chat_id), False))
                continue
            username = _chat_username(inspected_peer)
            invite_link = f"https://t.me/{username}" if username is not None else None
            metadata_error: str | None = None
            if chat_id < 0 and username is None:
                try:
                    async with self._request_limiter.slot(chat_id):
                        invite_link = await self._client.get_invite_link(inspected_peer)
                except Exception as error:
                    metadata_error = type(error).__name__
            inspections.append(
                ChatInspection(
                    ChatAccess(
                        chat_id,
                        title=_chat_title(inspected_peer),
                        username=username,
                        invite_link=invite_link,
                    ),
                    True,
                    metadata_error,
                )
            )
        return tuple(inspections)

    async def join_chat(self, access: ChatAccess) -> RebuildJoinResult:
        try:
            dialogs = await self._client.get_dialogs()
            for dialog in dialogs:
                peer = dialog.chat
                self._peers.remember(peer)
                if peer_dialog_id(peer) == access.chat_id:
                    return RebuildJoinResult.ALREADY_JOINED

            if access.username is not None:
                peer = await self._client.resolve_peer(f"@{access.username}")
                _require_expected_chat(peer, access.chat_id)
                peer = await self._client.join_channel(peer)
                self._peers.remember(peer)
                return RebuildJoinResult.JOINED

            invite_hash = _invite_hash(access.invite_link)
            if invite_hash is None:
                raise ValueError(f"chat {access.chat_id} has no usable join reference")
            joined = tuple(await self._client.join_chat_invite(invite_hash))
            for peer in joined:
                self._peers.remember(peer)
                if peer_dialog_id(peer) == access.chat_id:
                    return RebuildJoinResult.JOINED
            raise ValueError(f"Telegram invite did not resolve to expected chat {access.chat_id}")
        except Exception as error:
            if type(error).__name__ == "InviteRequestSentError":
                return RebuildJoinResult.APPROVAL_PENDING
            raise _translate_error(error) from error

    async def latest_message_id(self, source: SourceEndpoint) -> int:
        async with self._request_limiter.slot(source.chat_id):
            try:
                return await self._client.get_latest_message_id(await self._source_peer(source))
            except Exception as error:
                raise _translate_error(error) from error

    async def fetch_messages_after(
        self,
        source: SourceEndpoint,
        after_message_id: int,
        *,
        limit: int,
    ) -> Sequence[IncomingMessage]:
        async with self._request_limiter.slot(source.chat_id):
            try:
                native_messages = await self._client.get_messages_after(
                    await self._source_peer(source),
                    after_message_id,
                    limit=limit,
                )
                observed_at = datetime.now(UTC)
                normalized = []
                for message in native_messages:
                    self._peers.remember(message.chat)
                    self._peers.remember(message.sender)
                    normalized.append(normalize_message(message, observed_at))
                return tuple(normalized)
            except Exception as error:
                raise _translate_error(error) from error

    def chat_title(self, chat_id: int) -> str | None:
        peer = self._peers.get(chat_id)
        return _chat_title(peer) if peer is not None else None

    async def source_title(self, source: SourceEndpoint) -> str | None:
        group_title = self.chat_title(source.chat_id)
        if source.topic_id is None:
            return group_title
        topic_title = await self._topic_title(source)
        if topic_title is None:
            return group_title
        return f"{group_title}/{topic_title}" if group_title is not None else topic_title

    def is_forum(self, chat_id: int) -> bool:
        peer = self._peers.get(chat_id)
        return peer is not None and bool(getattr(peer, "forum", False))

    async def create_forum_topic(
        self,
        destination_chat_id: int,
        title: str,
        *,
        random_id: int,
    ) -> int:
        async with self._request_limiter.slot(destination_chat_id):
            try:
                return await self._client.create_forum_topic(
                    self._peer(destination_chat_id),
                    title,
                    random_id=random_id,
                )
            except Exception as error:
                raise _translate_error(error) from error

    async def edit_forum_topic(
        self,
        destination_chat_id: int,
        topic_id: int,
        *,
        title: str,
    ) -> None:
        async with self._request_limiter.slot(destination_chat_id):
            try:
                await self._client.edit_forum_topic(
                    self._peer(destination_chat_id),
                    topic_id,
                    title=title,
                )
            except Exception as error:
                if type(error).__name__ == "TopicNotModifiedError":
                    return
                raise _translate_error(error) from error

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

    async def _topic_title(self, source: SourceEndpoint) -> str | None:
        try:
            async with self._request_limiter.slot(source.chat_id):
                return await self._client.get_forum_topic_title(
                    self._peer(source.chat_id),
                    source.topic_id or 1,
                )
        except Exception:
            return None

    async def _forward(
        self,
        messages: Sequence[NativeMessage],
        target: NativePeer,
        destination: DestinationEndpoint,
        reply_to_message_id: int | None,
    ) -> Sequence[NativeMessage]:
        if reply_to_message_id is not None:
            raise NativeForwardUnsupported(
                "Telegram native forward cannot preserve an ordinary reply target"
            )
        if any(not message.can_forward for message in messages):
            raise NativeForwardUnsupported("source content has forwarding protection")
        source = messages[0].chat
        if any(peer_dialog_id(message.chat) != peer_dialog_id(source) for message in messages):
            raise ValueError("native forwarded messages must share a source chat")
        return await self._client.forward_messages(
            target,
            [message.id for message in messages],
            source,
            topic_id=destination.topic_id,
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
            data = BytesIO()
            await self._client.download(source.file, data)
            data.seek(0)
            if source.photo is not None:
                data.name = "photo.jpg"
                return await self._client.send_photo(
                    target,
                    data,
                    caption_html=source.text_html,
                    reply_to=reply_to,
                )
            data.name = cast(str | None, getattr(source.file, "name", None)) or "file.bin"
            return await self._client.send_file(
                target,
                data,
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

    async def _source_peer(self, source: SourceEndpoint) -> NativePeer:
        cached = self._peers.get(source.chat_id)
        if cached is not None:
            return cached

        references: tuple[int | str, ...] = (
            (source.chat_id, f"@{source.username}")
            if source.username is not None
            else (source.chat_id,)
        )
        last_error: Exception | None = None
        for reference in references:
            try:
                peer = await self._client.resolve_peer(reference)
            except Exception as error:
                last_error = error
                continue
            if peer_dialog_id(peer) != source.chat_id:
                last_error = ValueError(
                    f"public username now resolves to a different chat than {source.chat_id}"
                )
                continue
            self._peers.remember(peer)
            return peer
        if last_error is not None:
            raise last_error
        raise PermanentDeliveryError(f"cannot resolve source chat {source.chat_id}")


def _effective_reply(
    destination: DestinationEndpoint, reply_to_message_id: int | None
) -> int | None:
    return reply_to_message_id if reply_to_message_id is not None else destination.topic_id


def _chat_title(peer: NativePeer) -> str | None:
    title = getattr(peer, "title", None) or getattr(peer, "name", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    parts = (
        value.strip()
        for value in (getattr(peer, "first_name", None), getattr(peer, "last_name", None))
        if isinstance(value, str) and value.strip()
    )
    return " ".join(parts) or None


def _chat_username(peer: NativePeer) -> str | None:
    username = getattr(peer, "username", None)
    if isinstance(username, str) and username.strip():
        return username.strip().removeprefix("@")
    for item in getattr(peer, "usernames", ()) or ():
        value = getattr(item, "username", None)
        if bool(getattr(item, "active", True)) and isinstance(value, str) and value.strip():
            return value.strip().removeprefix("@")
    return None


def _require_expected_chat(peer: NativePeer, expected_chat_id: int) -> None:
    actual = peer_dialog_id(peer)
    if actual != expected_chat_id:
        raise ValueError(f"join reference resolves to chat {actual}, expected {expected_chat_id}")


def _invite_hash(link: str | None) -> str | None:
    if link is None:
        return None
    parsed = urlparse(link.strip())
    if parsed.scheme == "tg" and parsed.netloc == "join":
        values = parse_qs(parsed.query).get("invite", ())
        return values[0] if values and values[0] else None
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() not in {
        "t.me",
        "telegram.me",
        "www.t.me",
        "www.telegram.me",
    }:
        return None
    path = parsed.path.strip("/")
    if path.startswith("+") and len(path) > 1:
        return path[1:]
    prefix = "joinchat/"
    return path[len(prefix) :] if path.startswith(prefix) and len(path) > len(prefix) else None


def _public_username(reference: str) -> str | None:
    if reference.startswith("@") and len(reference) > 1:
        return reference[1:]
    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() not in {
        "t.me",
        "telegram.me",
        "www.t.me",
        "www.telegram.me",
    }:
        return None
    path = parsed.path.strip("/")
    if not path or "/" in path or path.startswith("+") or path.startswith("joinchat"):
        return None
    return path


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
    if name is None and error.__class__.__module__.startswith("telethon.errors"):
        name = error.__class__.__name__
        value = cast(int | None, getattr(error, "seconds", None))
    if name is not None:
        if (
            name.startswith("FLOOD_WAIT")
            or name.startswith("SLOWMODE_WAIT")
            or name in {"FloodWaitError", "SlowModeWaitError"}
        ):
            return RetryAfter(float(value or 1))
        if name in {"MESSAGE_NOT_MODIFIED", "MessageNotModifiedError"}:
            return MessageNotModified(str(error))
        if name in {"MESSAGE_ID_INVALID", "MSG_ID_INVALID", "MessageIdInvalidError"}:
            return MessageNotFound(str(error))
        if native_forward and name in {
            "CHAT_FORWARDS_RESTRICTED",
            "CHAT_SEND_MEDIA_FORBIDDEN",
            "USER_BANNED_IN_CHANNEL",
            "ChatForwardsRestrictedError",
            "ChatSendMediaForbiddenError",
            "UserBannedInChannelError",
        }:
            return NativeForwardUnsupported(str(error))
        return PermanentDeliveryError(f"Telegram RPC {name}: {error}")
    if isinstance(error, (OSError, TimeoutError, ConnectionError)):
        return RetryAfter(1)
    return error
