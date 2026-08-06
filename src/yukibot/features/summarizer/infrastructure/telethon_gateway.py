"""Telethon history and delivery adapter for the summarizer feature."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from yukibot.adapters.telegram.client import (
    NativeClient,
    NativeMessage,
    NativePeer,
    PeerRegistry,
    peer_dialog_id,
)
from yukibot.adapters.telegram.event_source import normalize_message
from yukibot.adapters.telegram.rate_limit import TelegramRequestLimiter
from yukibot.contracts import MessageRef, TelegramContentType

from ..errors import SummarizerError
from ..models import (
    FetchedSummaryMessages,
    SummaryChatKind,
    SummaryEndpoint,
    SummaryMessage,
)
from ..references import parse_endpoint_reference

_URL = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_MEDIA_LABELS = {
    TelegramContentType.PHOTO: "图片",
    TelegramContentType.VIDEO: "视频",
    TelegramContentType.DOCUMENT: "文件",
    TelegramContentType.AUDIO: "音频",
    TelegramContentType.VOICE: "语音",
    TelegramContentType.VIDEO_NOTE: "视频消息",
    TelegramContentType.STICKER: "贴纸",
    TelegramContentType.ANIMATION: "动画",
    TelegramContentType.LOCATION: "位置",
    TelegramContentType.CONTACT: "联系人",
    TelegramContentType.VENUE: "地点",
    TelegramContentType.GAME: "游戏",
}


class TelethonSummaryGateway:
    def __init__(
        self,
        client: NativeClient,
        peers: PeerRegistry,
        *,
        request_limiter: TelegramRequestLimiter | None = None,
    ) -> None:
        self._client = client
        self._peers = peers
        self._limiter = request_limiter or TelegramRequestLimiter()

    async def resolve_endpoint(self, reference: str) -> SummaryEndpoint:
        parsed = parse_endpoint_reference(reference)
        try:
            peer = self._peers.get(parsed.chat) if isinstance(parsed.chat, int) else None
            if peer is None:
                peer = await self._client.resolve_peer(parsed.chat)
                self._peers.remember(peer)
            chat_id = peer_dialog_id(peer)
            if isinstance(parsed.chat, int) and chat_id != parsed.chat:
                raise ValueError(
                    f"Telegram reference resolves to chat {chat_id}, expected {parsed.chat}"
                )
            if parsed.topic_id is not None and not bool(getattr(peer, "forum", False)):
                raise ValueError("指定了话题 ID, 但目标聊天不是论坛群组")
            return SummaryEndpoint(chat_id, parsed.topic_id, _chat_username(peer))
        except ValueError:
            raise
        except Exception as error:
            raise SummarizerError(f"无法解析 Telegram 聊天: {error}") from error

    async def fetch_recent(
        self,
        source: SummaryEndpoint,
        *,
        since: datetime,
        limit: int,
    ) -> FetchedSummaryMessages:
        peer = await self._endpoint_peer(source)
        try:
            async with self._limiter.slot(source.chat_id):
                native_messages = await self._client.get_messages_recent(
                    peer,
                    since=since,
                    limit=limit,
                    topic_id=source.topic_id,
                )
        except Exception as error:
            raise SummarizerError(f"读取 Telegram 历史消息失败: {error}") from error
        observed_at = datetime.now(UTC)
        messages = tuple(
            normalized
            for message in native_messages
            if (normalized := _summary_message(message, observed_at)) is not None
        )
        return FetchedSummaryMessages(
            source,
            _chat_kind(peer, source.chat_id),
            _chat_title(peer) or str(source.chat_id),
            messages,
        )

    async def send_text(self, destination: SummaryEndpoint, text: str) -> MessageRef:
        peer = await self._endpoint_peer(destination)
        try:
            async with self._limiter.slot(destination.chat_id):
                sent = await self._client.send_message(
                    peer,
                    text,
                    reply_to=destination.topic_id,
                )
        except Exception as error:
            raise SummarizerError(f"发送 Telegram 总结失败: {error}") from error
        return MessageRef(peer_dialog_id(sent.chat), sent.id)

    async def _endpoint_peer(self, endpoint: SummaryEndpoint) -> NativePeer:
        cached = self._peers.get(endpoint.chat_id)
        if cached is not None:
            return cached
        references: tuple[int | str, ...] = (
            (endpoint.chat_id, f"@{endpoint.username}")
            if endpoint.username is not None
            else (endpoint.chat_id,)
        )
        last_error: Exception | None = None
        for reference in references:
            try:
                peer = await self._client.resolve_peer(reference)
            except Exception as error:
                last_error = error
                continue
            if peer_dialog_id(peer) != endpoint.chat_id:
                last_error = ValueError(
                    f"Telegram username now resolves to a different chat than {endpoint.chat_id}"
                )
                continue
            self._peers.remember(peer)
            return peer
        raise SummarizerError(f"无法访问 Telegram 聊天 {endpoint.chat_id}: {last_error}")


def _summary_message(message: NativeMessage, observed_at: datetime) -> SummaryMessage | None:
    common = normalize_message(message, observed_at)
    if common.content_type is TelegramContentType.SERVICE:
        return None
    text = (common.text or common.caption or "").strip()
    if common.content_type is TelegramContentType.POLL:
        text = _poll_text(message._raw) or text
    if not text:
        return None
    label = _MEDIA_LABELS.get(common.content_type)
    if label is not None:
        text = f"[{label}] {text}"
    sender_name = _chat_title(message.sender) if message.sender is not None else None
    if sender_name is None:
        sender_name = str(message.sender_id) if message.sender_id is not None else "未知发送者"
    return SummaryMessage(
        (common.ref,),
        common.occurred_at,
        sender_name,
        text,
        common.sender_id,
        common.reply_to_message_id,
        common.grouped_id,
        _forwarded_from(message._raw),
        _message_links(message),
    )


def _poll_text(raw: object) -> str | None:
    media = getattr(raw, "media", None)
    poll = getattr(media, "poll", None)
    question = getattr(poll, "question", None)
    if not isinstance(question, str) or not question.strip():
        return None
    options = []
    for answer in getattr(poll, "answers", ()) or ():
        value = getattr(answer, "text", None)
        if isinstance(value, str) and value.strip():
            options.append(value.strip())
    suffix = f" 选项: {'; '.join(options)}" if options else ""
    return f"[投票] {question.strip()}{suffix}"


def _message_links(message: NativeMessage) -> tuple[str, ...]:
    links = list(_URL.findall(message.text or ""))
    for entity in getattr(message._raw, "entities", ()) or ():
        url = getattr(entity, "url", None)
        if isinstance(url, str) and url.strip():
            links.append(url.strip())
    return tuple(dict.fromkeys(links))


def _forwarded_from(raw: object) -> str | None:
    header = getattr(raw, "fwd_from", None)
    if header is None:
        return None
    for name in ("from_name", "post_author", "saved_from_name"):
        value = getattr(header, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Telegram 转发消息"


def _chat_kind(peer: NativePeer, chat_id: int) -> SummaryChatKind:
    if chat_id > 0:
        return SummaryChatKind.PRIVATE
    if bool(getattr(peer, "broadcast", False)) and not bool(getattr(peer, "megagroup", False)):
        return SummaryChatKind.CHANNEL
    return SummaryChatKind.GROUP


def _chat_title(peer: object | None) -> str | None:
    if peer is None:
        return None
    title = getattr(peer, "title", None) or getattr(peer, "name", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    parts = [
        value.strip()
        for value in (getattr(peer, "first_name", None), getattr(peer, "last_name", None))
        if isinstance(value, str) and value.strip()
    ]
    return " ".join(parts) or None


def _chat_username(peer: NativePeer) -> str | None:
    username = getattr(peer, "username", None)
    if isinstance(username, str) and username.strip():
        return username.strip().removeprefix("@")
    return None
