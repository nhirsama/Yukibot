import asyncio
from datetime import UTC, datetime

from conftest import FakeTelegramGateway

from yukibot.features.forwarder import (
    ContentType,
    DestinationEndpoint,
    Forwarder,
    ForwarderService,
    IncomingMessage,
    InMemoryMessageLinkRepository,
    InMemoryRouteRepository,
    MessageRef,
    Route,
    SourceEndpoint,
)


async def test_standalone_facade_assembles_canonical_grouped_messages() -> None:
    service = ForwarderService(
        InMemoryRouteRepository([Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001))]),
        InMemoryMessageLinkRepository(),
        FakeTelegramGateway(),
    )
    reports = []

    async def record(report):  # type: ignore[no-untyped-def]
        reports.append(report)

    forwarder = Forwarder(service, album_flush_delay=0.01, on_background_report=record)
    for message_id in (11, 10):
        result = await forwarder.handle_message(
            IncomingMessage(
                MessageRef(-1001, message_id),
                ContentType.PHOTO,
                datetime.now(UTC),
                grouped_id=50,
                caption="album",
            )
        )
        assert result.buffered

    await asyncio.sleep(0.03)
    await forwarder.close()

    assert reports[0].delivered_messages == 2
