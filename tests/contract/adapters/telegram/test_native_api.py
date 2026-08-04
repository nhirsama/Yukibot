import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import telethon
from telethon import TelegramClient, events
from telethon.tl import types
from telethon.tl.custom.message import Message

from yukibot.adapters.telegram import (
    TelethonClientAdapter,
    create_telethon_client,
    normalize_message,
)
from yukibot.adapters.telegram.client import telethon_event_types


def test_stable_telethon_api_matches_adapter(tmp_path: Path) -> None:
    client = create_telethon_client(tmp_path / "contract.session", 1, "hash")

    assert telethon.__version__ == "1.44.0"
    assert isinstance(client, TelethonClientAdapter)
    assert isinstance(client.native_client, TelegramClient)
    assert client.native_client._init_request.device_model == "Yukibot"  # type: ignore[attr-defined]
    assert telethon_event_types() == (
        events.NewMessage,
        events.MessageEdited,
        events.MessageDeleted,
    )


async def test_stable_telethon_message_reaches_the_application_contract(tmp_path: Path) -> None:
    client = create_telethon_client(tmp_path / "message.session", 1, "hash")
    assert isinstance(client, TelethonClientAdapter)
    raw_client = client.native_client
    message = Message(
        id=7,
        peer_id=types.PeerChannel(4348538590),
        date=datetime(2026, 8, 5, tzinfo=UTC),
        message="/admin admin list",
        out=True,
        from_id=types.PeerUser(8919848716),
    )
    message._finish_init(
        raw_client,
        {},
        types.InputPeerChannel(4348538590, 123),
    )
    received = []

    async def handle(event):  # type: ignore[no-untyped-def]
        received.append(event)

    client.add_event_handler(handle, events.NewMessage)
    await client._handlers[handle][0](message)
    client.remove_event_handler(handle)
    raw_client.session.close()  # type: ignore[attr-defined]

    normalized = normalize_message(received[0], datetime.now(UTC))
    assert normalized.ref.chat_id == -1004348538590
    assert normalized.sender_id == 8919848716
    assert normalized.outgoing
    assert normalized.text == "/admin admin list"
    assert handle not in client._handlers
    assert all(
        hasattr(TelegramClient, method)
        for method in (
            "add_event_handler",
            "remove_event_handler",
            "run_until_disconnected",
            "get_me",
            "get_messages",
            "forward_messages",
            "send_file",
            "download_media",
        )
    )


def test_v2_alpha_session_is_migrated_without_losing_authorization(tmp_path: Path) -> None:
    session_path = tmp_path / "legacy.session"
    authorization_key = bytes(range(256))
    with sqlite3.connect(session_path) as connection:
        connection.executescript(
            """
            CREATE TABLE version (version INTEGER PRIMARY KEY);
            INSERT INTO version VALUES (10);
            CREATE TABLE datacenter (
                id INTEGER PRIMARY KEY,
                ipv4_addr TEXT,
                ipv6_addr TEXT,
                auth BLOB
            );
            CREATE TABLE user (id INTEGER PRIMARY KEY, dc INTEGER, bot INTEGER, username TEXT);
            CREATE TABLE state (pts INTEGER, qts INTEGER, date INTEGER, seq INTEGER);
            """
        )
        connection.execute(
            "INSERT INTO datacenter VALUES (?, ?, ?, ?)",
            (5, "91.108.56.197:443", None, authorization_key),
        )
        connection.execute("INSERT INTO user VALUES (8919848716, 5, 0, 'owner')")
        connection.execute("INSERT INTO state VALUES (11, 12, 1700000000, 13)")

    client = create_telethon_client(session_path, 1, "hash")
    assert isinstance(client, TelethonClientAdapter)
    client.native_client.session.close()  # type: ignore[attr-defined]

    with sqlite3.connect(session_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        session = connection.execute(
            "SELECT dc_id, server_address, port, auth_key FROM sessions"
        ).fetchone()
        state = connection.execute(
            "SELECT pts, qts, date, seq FROM update_state WHERE id = 0"
        ).fetchone()

    assert "sessions" in tables
    assert session == (5, "91.108.56.197", 443, authorization_key)
    assert state == (11, 12, 1700000000, 13)
    assert (tmp_path / "legacy.session.v2.bak").exists()
