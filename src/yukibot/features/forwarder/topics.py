"""Idempotent creation and title synchronization for route-owned forum topics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from hashlib import sha256

from .errors import PermanentDeliveryError
from .models import DestinationEndpoint, ManagedTopic, Route
from .ports import ManagedTopicRepository, TelegramGateway


@dataclass(slots=True)
class ManagedTopicService:
    repository: ManagedTopicRepository
    telegram: TelegramGateway
    _locks: dict[tuple[int, int], asyncio.Lock] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    async def resolve(
        self,
        route: Route,
        *,
        source_title: str | None = None,
    ) -> DestinationEndpoint:
        """Return an explicit destination, creating or renaming an automatic topic."""

        if route.destination.topic_id is not None:
            return route.destination
        if not self.telegram.is_forum(route.destination.chat_id):
            return route.destination

        key = (route.source.chat_id, route.destination.chat_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            existing = await self.repository.get(*key)
            if existing is not None:
                # topic_id is the durable identity. Ordinary messages only reuse it;
                # a title change must come from an explicit management or service event.
                title = _topic_title(source_title) if source_title is not None else existing.title
                if title != existing.title:
                    await self.telegram.edit_forum_topic(
                        existing.destination_chat_id,
                        existing.topic_id,
                        title=title,
                    )
                    existing = ManagedTopic(*key, existing.topic_id, title)
                    await self.repository.save(existing)
                return DestinationEndpoint(existing.destination_chat_id, existing.topic_id)

            raw_title = source_title or self.telegram.chat_title(route.source.chat_id)
            if raw_title is None or not raw_title.strip():
                raise PermanentDeliveryError(
                    f"cannot create an automatic topic because chat "
                    f"{route.source.chat_id} has no resolved title"
                )
            title = _topic_title(raw_title)
            topic_id = await self.telegram.create_forum_topic(
                route.destination.chat_id,
                title,
                random_id=_creation_random_id(*key),
            )
            topic = ManagedTopic(*key, topic_id, title)
            await self.repository.save(topic)
            return DestinationEndpoint(topic.destination_chat_id, topic.topic_id)


def _topic_title(value: str) -> str:
    title = value.strip()
    if not title:
        raise ValueError("topic title must not be empty")
    encoded = title.encode("utf-8")
    if len(encoded) <= 128:
        return title
    return encoded[:128].decode("utf-8", errors="ignore").rstrip()


def _creation_random_id(source_chat_id: int, destination_chat_id: int) -> int:
    payload = f"yukibot-topic:{source_chat_id}:{destination_chat_id}".encode()
    value = int.from_bytes(sha256(payload).digest()[:8], "big", signed=True)
    return value or 1
