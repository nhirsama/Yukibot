from pathlib import Path

import telethon
from telethon import Client, events

from yukibot.adapters.telegram import create_telethon_client
from yukibot.adapters.telegram.client import telethon_event_types


def test_pinned_telethon_v2_api_matches_adapter(tmp_path: Path) -> None:
    client = create_telethon_client(tmp_path / "contract.session", 1, "hash")

    assert telethon.__version__ == "2.0.0a0"
    assert isinstance(client, Client)
    assert telethon_event_types() == (
        events.NewMessage,
        events.MessageEdited,
        events.MessageDeleted,
    )
    assert all(
        hasattr(Client, method)
        for method in (
            "add_event_handler",
            "remove_event_handler",
            "get_messages_with_ids",
            "forward_messages",
            "prepare_album",
            "send_file",
            "download",
        )
    )
