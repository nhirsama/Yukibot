from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from yukibot.features.forwarder import (
    DestinationEndpoint,
    ForwardMode,
    IncomingMessage,
    MessageNotModified,
    MessageRef,
    NativeForwardUnsupported,
)


@dataclass(frozen=True)
class DeliveryCall:
    messages: tuple[IncomingMessage, ...]
    destination: DestinationEndpoint
    mode: ForwardMode
    reply_to_message_id: int | None


class FakeTelegramGateway:
    def __init__(self) -> None:
        self.calls: list[DeliveryCall] = []
        self.sent_texts: list[tuple[str, DestinationEndpoint, int | None]] = []
        self.edits: list[tuple[IncomingMessage, MessageRef]] = []
        self.deletes: list[MessageRef] = []
        self.next_message_id = 1000
        self.reject_native_forward = False
        self.not_modified_targets: set[MessageRef] = set()
        self.chat_titles: dict[int, str] = {}
        self.forum_chats: set[int] = set()
        self.created_topics: list[tuple[int, str, int]] = []
        self.edited_topics: list[tuple[int, int, str]] = []
        self.next_topic_id = 500

    def chat_title(self, chat_id: int) -> str:
        return self.chat_titles.get(chat_id, str(chat_id))

    def is_forum(self, chat_id: int) -> bool:
        return chat_id in self.forum_chats

    async def create_forum_topic(
        self,
        destination_chat_id: int,
        title: str,
        *,
        random_id: int,
    ) -> int:
        self.created_topics.append((destination_chat_id, title, random_id))
        topic_id = self.next_topic_id
        self.next_topic_id += 1
        return topic_id

    async def edit_forum_topic(
        self,
        destination_chat_id: int,
        topic_id: int,
        *,
        title: str,
    ) -> None:
        self.edited_topics.append((destination_chat_id, topic_id, title))

    async def deliver_message(
        self,
        message: IncomingMessage,
        destination: DestinationEndpoint,
        *,
        mode: ForwardMode,
        reply_to_message_id: int | None,
    ) -> MessageRef:
        self.calls.append(DeliveryCall((message,), destination, mode, reply_to_message_id))
        if mode is ForwardMode.FORWARD and self.reject_native_forward:
            raise NativeForwardUnsupported
        return self._next_ref(destination)

    async def deliver_album(
        self,
        messages: Sequence[IncomingMessage],
        destination: DestinationEndpoint,
        *,
        mode: ForwardMode,
        reply_to_message_id: int | None,
    ) -> Sequence[MessageRef]:
        self.calls.append(DeliveryCall(tuple(messages), destination, mode, reply_to_message_id))
        if mode is ForwardMode.FORWARD and self.reject_native_forward:
            raise NativeForwardUnsupported
        return tuple(self._next_ref(destination) for _ in messages)

    async def send_text(
        self,
        text: str,
        destination: DestinationEndpoint,
        *,
        reply_to_message_id: int | None,
    ) -> MessageRef:
        self.sent_texts.append((text, destination, reply_to_message_id))
        return self._next_ref(destination)

    async def edit_from_source(self, source: IncomingMessage, target: MessageRef) -> None:
        if target in self.not_modified_targets:
            raise MessageNotModified
        self.edits.append((source, target))

    async def delete_message(self, target: MessageRef) -> None:
        self.deletes.append(target)

    def _next_ref(self, destination: DestinationEndpoint) -> MessageRef:
        result = MessageRef(destination.chat_id, self.next_message_id)
        self.next_message_id += 1
        return result
