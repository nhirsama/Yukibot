from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO


class FakePeerId:
    def __init__(self, value: int) -> None:
        self.value = value

    def __int__(self) -> int:
        return self.value


@dataclass
class FakePeer:
    value: int
    name: str = "peer"
    forum: bool = False
    username: str | None = None
    left: bool = False

    @property
    def id(self) -> FakePeerId:
        return FakePeerId(self.value)


class MessageMediaEmpty:
    pass


@dataclass
class FakeRaw:
    media: object = field(default_factory=MessageMediaEmpty)
    reply_to: object | None = None
    action: object | None = None
    edit_date: object | None = None


@dataclass
class FakeMessage:
    id: int
    chat: FakePeer
    text: str | None = "hello"
    text_html: str | None = "<strong>hello</strong>"
    date: object | None = field(default_factory=lambda: datetime.now(UTC))
    sender: FakePeer | None = None
    grouped_id: int | None = None
    photo: object | None = None
    audio: object | None = None
    video: object | None = None
    file: object | None = None
    replied_message_id: int | None = None
    outgoing: bool = False
    can_forward: bool = True
    _raw: object = field(default_factory=FakeRaw)

    @property
    def chat_id(self) -> int:
        return self.chat.value

    @property
    def sender_id(self) -> int | None:
        return self.sender.value if self.sender is not None else None


@dataclass
class FakeDialog:
    chat: FakePeer


class FakeAlbum:
    def __init__(self, client: FakeNativeClient) -> None:
        self.client = client
        self.items: list[tuple[str, bytes, str | None]] = []
        self.reply_to: int | None = None

    async def add_photo(self, file: BytesIO, *, caption_html: str | None = None) -> None:
        self.items.append(("photo", file.read(), caption_html))

    async def add_video(self, file: BytesIO, *, caption_html: str | None = None) -> None:
        self.items.append(("video", file.read(), caption_html))

    async def send(self, peer: FakePeer, *, reply_to: int | None = None):  # type: ignore[no-untyped-def]
        self.reply_to = reply_to
        return tuple(self.client.sent(peer) for _ in self.items)


class FakeNativeClient:
    def __init__(self) -> None:
        self.handlers: dict[type[object], object] = {}
        self.update_pump_calls = 0
        self.update_pump_started = asyncio.Event()
        self.update_pump_stopped = asyncio.Event()
        self._disconnect_requested = asyncio.Event()
        self.connected = False
        self.disconnected = False
        self.authorized = True
        self.logged_in = False
        self.dialogs: list[FakeDialog] = []
        self.me = FakePeer(999, "owner")
        self.messages: dict[tuple[int, int], FakeMessage] = {}
        self.calls: list[tuple[object, ...]] = []
        self.next_id = 100
        self.error: Exception | None = None
        self.album: FakeAlbum | None = None
        self.resolved: dict[int | str, FakePeer] = {}
        self.invite_links: dict[int, str] = {}
        self.invite_checks: dict[str, FakePeer | None] = {}
        self.invite_joins: dict[str, tuple[FakePeer, ...]] = {}
        self.invite_join_errors: dict[str, Exception] = {}

    def add_event_handler(self, handler, event_cls):  # type: ignore[no-untyped-def]
        self.handlers[event_cls] = handler

    def remove_event_handler(self, handler):  # type: ignore[no-untyped-def]
        for event_type, registered in tuple(self.handlers.items()):
            if registered == handler:
                self.handlers.pop(event_type)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False
        self._disconnect_requested.set()

    async def run_until_disconnected(self) -> None:
        self.update_pump_calls += 1
        self.update_pump_started.set()
        try:
            await self._disconnect_requested.wait()
        finally:
            self.update_pump_stopped.set()

    async def is_authorized(self) -> bool:
        return self.authorized

    async def interactive_login(self) -> object:
        self.logged_in = True
        self.authorized = True
        return object()

    async def get_me(self) -> FakePeer:
        return self.me

    async def get_dialogs(self):  # type: ignore[no-untyped-def]
        return tuple(self.dialogs)

    async def get_messages_with_ids(self, chat: FakePeer, message_ids: list[int]):  # type: ignore[no-untyped-def]
        return tuple(
            self.messages[(int(chat.id), message_id)]
            for message_id in message_ids
            if (int(chat.id), message_id) in self.messages
        )

    async def resolve_peer(self, reference: int | str) -> FakePeer:
        self.calls.append(("resolve", reference))
        return self.resolved[reference]

    async def join_channel(self, peer: FakePeer) -> FakePeer:
        self.calls.append(("join", int(peer.id)))
        peer.left = False
        return peer

    async def get_invite_link(self, peer: FakePeer) -> str | None:
        self.calls.append(("invite-link", int(peer.id)))
        return self.invite_links.get(int(peer.id))

    async def check_chat_invite(self, invite_hash: str) -> FakePeer | None:
        self.calls.append(("check-invite", invite_hash))
        return self.invite_checks.get(invite_hash)

    async def join_chat_invite(self, invite_hash: str) -> tuple[FakePeer, ...]:
        self.calls.append(("join-invite", invite_hash))
        if error := self.invite_join_errors.get(invite_hash):
            raise error
        return self.invite_joins[invite_hash]

    async def get_latest_message_id(self, chat: FakePeer) -> int:
        self.calls.append(("latest", int(chat.id)))
        return max(
            (message_id for chat_id, message_id in self.messages if chat_id == int(chat.id)),
            default=0,
        )

    async def get_messages_after(
        self,
        chat: FakePeer,
        after_message_id: int,
        *,
        limit: int,
    ) -> tuple[FakeMessage, ...]:
        self.calls.append(("history", int(chat.id), after_message_id, limit))
        return tuple(
            self.messages[key]
            for key in sorted(self.messages)
            if key[0] == int(chat.id) and key[1] > after_message_id
        )[:limit]

    async def forward_messages(
        self,
        target,
        message_ids,
        source,
        *,
        topic_id=None,  # type: ignore[no-untyped-def]
    ):
        self.raise_error()
        self.calls.append(("forward", int(target.id), tuple(message_ids), int(source.id), topic_id))
        return tuple(self.sent(target) for _ in message_ids)

    async def create_forum_topic(
        self,
        chat,
        title,
        *,
        random_id,  # type: ignore[no-untyped-def]
    ):
        self.raise_error()
        self.calls.append(("topic-create", int(chat.id), title, random_id))
        topic_id = self.next_id
        self.next_id += 1
        return topic_id

    async def edit_forum_topic(
        self,
        chat,
        topic_id,
        *,
        title,  # type: ignore[no-untyped-def]
    ) -> None:
        self.raise_error()
        self.calls.append(("topic-edit", int(chat.id), topic_id, title))

    async def send_message(
        self,
        chat,
        text=None,
        *,
        html=None,
        reply_to=None,  # type: ignore[no-untyped-def]
    ):
        self.raise_error()
        self.calls.append(("message", int(chat.id), text, html, reply_to))
        return self.sent(chat)

    async def send_photo(
        self,
        chat,
        file,
        *,
        caption_html=None,
        reply_to=None,  # type: ignore[no-untyped-def]
    ):
        self.raise_error()
        self.calls.append(("photo", int(chat.id), file, caption_html, reply_to))
        return self.sent(chat)

    async def send_file(
        self,
        chat,
        file,
        *,
        caption_html=None,
        reply_to=None,  # type: ignore[no-untyped-def]
    ):
        self.raise_error()
        self.calls.append(("file", int(chat.id), file, caption_html, reply_to))
        return self.sent(chat)

    async def edit_message(
        self,
        chat,
        message_id,
        *,
        text=None,
        html=None,  # type: ignore[no-untyped-def]
    ):
        self.raise_error()
        self.calls.append(("edit", int(chat.id), message_id, text, html))
        return self.sent(chat)

    async def delete_messages(self, chat, message_ids, *, revoke=True):  # type: ignore[no-untyped-def]
        self.raise_error()
        self.calls.append(("delete", int(chat.id), tuple(message_ids), revoke))
        return len(message_ids)

    async def download(self, media: object, file: BytesIO) -> None:
        self.calls.append(("download", media))
        file.write(b"media-bytes")

    def prepare_album(self) -> FakeAlbum:
        self.album = FakeAlbum(self)
        return self.album

    def sent(self, chat: FakePeer) -> FakeMessage:
        message = FakeMessage(self.next_id, chat)
        self.next_id += 1
        return message

    def raise_error(self) -> None:
        if self.error is not None:
            raise self.error
