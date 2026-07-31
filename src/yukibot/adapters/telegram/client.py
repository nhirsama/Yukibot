"""Narrow native-client protocol and the only Telethon client factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from importlib import import_module
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, cast


class NativePeerId(Protocol):
    def __int__(self) -> int: ...


class NativePeer(Protocol):
    @property
    def id(self) -> NativePeerId: ...

    @property
    def name(self) -> str: ...


class NativeMessage(Protocol):
    id: int
    grouped_id: int | None
    text: str | None
    text_html: str | None
    date: object | None
    chat: NativePeer
    sender: NativePeer | None
    photo: object | None
    audio: object | None
    video: object | None
    file: object | None
    replied_message_id: int | None
    outgoing: bool
    can_forward: bool
    _raw: object


class NativeDeletedEvent(Protocol):
    message_ids: Sequence[int]
    channel_id: int | None


class NativeDialog(Protocol):
    chat: NativePeer


class NativeAlbum(Protocol):
    async def add_photo(self, file: BytesIO, *, caption_html: str | None = None) -> None: ...

    async def add_video(self, file: BytesIO, *, caption_html: str | None = None) -> None: ...

    async def send(
        self, peer: object, *, reply_to: int | None = None
    ) -> Sequence[NativeMessage]: ...


type NativeHandler = Callable[[object], Awaitable[object]]


class NativeClient(Protocol):
    def add_event_handler(self, handler: NativeHandler, event_cls: type[object]) -> None: ...

    def remove_event_handler(self, handler: NativeHandler) -> None: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_authorized(self) -> bool: ...

    async def interactive_login(self) -> object: ...

    def get_dialogs(self) -> Awaitable[Sequence[NativeDialog]]: ...

    def get_messages_with_ids(
        self, chat: object, message_ids: list[int]
    ) -> Awaitable[Sequence[NativeMessage]]: ...

    async def forward_messages(
        self, target: object, message_ids: list[int], source: object
    ) -> Sequence[NativeMessage]: ...

    async def send_message(
        self,
        chat: object,
        text: str | None = None,
        *,
        html: str | None = None,
        reply_to: int | None = None,
    ) -> NativeMessage: ...

    async def send_photo(
        self,
        chat: object,
        file: object,
        *,
        caption_html: str | None = None,
        reply_to: int | None = None,
    ) -> NativeMessage: ...

    async def send_file(
        self,
        chat: object,
        file: object,
        *,
        caption_html: str | None = None,
        reply_to: int | None = None,
    ) -> NativeMessage: ...

    async def edit_message(
        self,
        chat: object,
        message_id: int,
        *,
        text: str | None = None,
        html: str | None = None,
    ) -> NativeMessage: ...

    async def delete_messages(
        self, chat: object, message_ids: list[int], *, revoke: bool = True
    ) -> int: ...

    async def download(self, media: object, file: BytesIO) -> None: ...

    def prepare_album(self) -> NativeAlbum: ...


class PeerRegistry:
    """In-memory authorization-aware peers indexed by Bot API dialog ID."""

    def __init__(self) -> None:
        self._peers: dict[int, NativePeer] = {}

    def remember(self, peer: NativePeer | None) -> None:
        if peer is not None:
            self._peers[peer_dialog_id(peer)] = peer

    def get(self, chat_id: int) -> NativePeer | None:
        return self._peers.get(chat_id)

    def clear(self) -> None:
        self._peers.clear()


class TelethonClientLifecycle:
    """Own disconnection separately so update intake can stop before feature draining."""

    name = "telegram-client"

    def __init__(self, client: NativeClient, peers: PeerRegistry) -> None:
        self._client = client
        self._peers = peers

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        try:
            await self._client.disconnect()
        finally:
            self._peers.clear()


def create_telethon_client(
    session_path: Path,
    api_id: int,
    api_hash: str,
) -> NativeClient:
    """Create the pinned Telethon v2 client without leaking its type elsewhere."""

    session_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
    telethon = import_module("telethon")
    client_type = cast(Any, telethon).Client
    client = client_type(
        session_path,
        api_id,
        api_hash,
        catch_up=True,
        check_all_handlers=True,
        flood_sleep_threshold=0,
    )
    return cast(NativeClient, client)


def telethon_event_types() -> tuple[type[object], type[object], type[object]]:
    events = cast(Any, import_module("telethon.events"))
    return events.NewMessage, events.MessageEdited, events.MessageDeleted


def peer_dialog_id(peer: NativePeer) -> int:
    """Normalize both pre- and post-Rust Telethon v2 peers to Bot API dialog IDs."""

    value = int(peer.id)
    if value <= 0:
        return value
    peer_name = type(peer).__name__
    reference_name = type(getattr(peer, "ref", None)).__name__
    if peer_name == "Channel" or reference_name == "ChannelRef":
        return -(1_000_000_000_000 + value)
    if peer_name == "Group" or reference_name == "GroupRef":
        return -value
    return value
