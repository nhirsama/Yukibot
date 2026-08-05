from pathlib import Path

import pytest

from yukibot.adapters.database import MigrationRunner, SqliteDatabase
from yukibot.features.forwarder import (
    ContentType,
    DestinationEndpoint,
    ForwardMode,
    ManagedTopic,
    MessageFilter,
    MessageLink,
    MessageRef,
    PollCursor,
    Route,
    RouteCycleError,
    RouteDraft,
    SourceEndpoint,
)
from yukibot.features.forwarder.migrations import FORWARDER_MIGRATIONS
from yukibot.features.forwarder.repository import (
    SqliteManagedTopicRepository,
    SqliteMessageLinkRepository,
    SqlitePollCursorRepository,
    SqliteRouteRepository,
)


def database_url(path: Path) -> str:
    return f"sqlite:///{path}"


async def open_repositories(
    path: Path,
) -> tuple[SqliteDatabase, SqliteRouteRepository, SqliteMessageLinkRepository]:
    database = SqliteDatabase(database_url(path))
    await database.open()
    await MigrationRunner(database, FORWARDER_MIGRATIONS).upgrade()
    return database, SqliteRouteRepository(database), SqliteMessageLinkRepository(database)


def route(route_id: int, source: int, destination: int) -> Route:
    return Route(
        route_id,
        SourceEndpoint(
            source,
            topic_id=7,
            username="source_channel",
            poll_interval_seconds=300,
        ),
        DestinationEndpoint(destination, topic_id=11, username="target_group"),
        mode=ForwardMode.FORWARD,
        message_filter=MessageFilter(
            keywords=("Python",),
            allowed_content_types=frozenset({ContentType.TEXT, ContentType.PHOTO}),
            blocked_content_types=frozenset({ContentType.PHOTO}),
            include_service_messages=True,
        ),
        fallback_to_copy=False,
    )


async def test_route_repository_round_trip_and_management(tmp_path: Path) -> None:
    database, routes, _ = await open_repositories(tmp_path / "routes.db")
    original = route(1, -1001, -1002)
    try:
        await routes.add(original)
        assert await routes.list_all() == (original,)
        assert await routes.list_for_source_chat(-1001) == (original,)
        assert await routes.list_for_source_chat(-9999) == ()

        with pytest.raises(ValueError, match="already exists"):
            await routes.add(original)

        replacement = Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2000))
        await routes.replace(replacement)
        assert await routes.list_all() == (replacement,)
        with pytest.raises(KeyError):
            await routes.replace(Route(99, SourceEndpoint(-1), DestinationEndpoint(-2)))

        assert await routes.remove(1)
        assert not await routes.remove(1)
    finally:
        await database.close()


async def test_route_repository_allocates_id(tmp_path: Path) -> None:
    database, routes, _ = await open_repositories(tmp_path / "auto-id.db")
    try:
        await routes.add(Route(7, SourceEndpoint(-7001), DestinationEndpoint(-7002)))
        generated = await routes.add_auto(
            RouteDraft(SourceEndpoint(-1001), DestinationEndpoint(-2001))
        )

        assert generated.id == 8
        assert await routes.list_all() == (
            Route(7, SourceEndpoint(-7001), DestinationEndpoint(-7002)),
            generated,
        )
    finally:
        await database.close()


async def test_route_cycle_is_rejected_without_partial_write(tmp_path: Path) -> None:
    database, routes, _ = await open_repositories(tmp_path / "cycle.db")
    first = Route(1, SourceEndpoint(-1001), DestinationEndpoint(-1002))
    cycle = Route(2, SourceEndpoint(-1002), DestinationEndpoint(-1001))
    try:
        await routes.add(first)
        with pytest.raises(RouteCycleError):
            await routes.add(cycle)
        assert await routes.list_all() == (first,)
    finally:
        await database.close()


async def test_message_links_are_upserted_queried_and_cascade_deleted(tmp_path: Path) -> None:
    database, routes, links = await open_repositories(tmp_path / "links.db")
    await routes.add(route(1, -1001, -1002))
    source = MessageRef(-1001, 10)
    original = MessageLink(1, source, MessageRef(-1002, 20))
    replacement = MessageLink(1, source, MessageRef(-1002, 21))
    try:
        await links.save_many((original,))
        await links.save_many((replacement,))

        assert await links.get(1, source) == replacement
        assert await links.find_all(source) == (replacement,)
        assert await links.find_by_source_message_id(10) == (replacement,)

        await routes.remove(1)
        assert await links.get(1, source) is None
    finally:
        await database.close()


async def test_managed_topics_are_upserted_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "topics.db"
    database, _, _ = await open_repositories(path)
    topics = SqliteManagedTopicRepository(database)
    original = ManagedTopic(-1001, -2001, 50, "Source channel")
    renamed = ManagedTopic(-1001, -2001, 50, "Renamed channel")
    try:
        assert await topics.get(-1001, -2001) is None
        await topics.save(original)
        await topics.save(renamed)
        assert await topics.get(-1001, -2001) == renamed
    finally:
        await database.close()

    reopened = SqliteDatabase(database_url(path))
    await reopened.open()
    try:
        assert await SqliteManagedTopicRepository(reopened).get(-1001, -2001) == renamed
    finally:
        await reopened.close()


async def test_poll_cursor_only_moves_forward_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "polling.db"
    database, _, _ = await open_repositories(path)
    cursors = SqlitePollCursorRepository(database)
    try:
        await cursors.save(PollCursor(-1001, 20))
        await cursors.save(PollCursor(-1001, 10))
        assert await cursors.get(-1001) == PollCursor(-1001, 20)
    finally:
        await database.close()

    reopened = SqliteDatabase(database_url(path))
    await reopened.open()
    try:
        assert await SqlitePollCursorRepository(reopened).get(-1001) == PollCursor(-1001, 20)
    finally:
        await reopened.close()


async def test_v3_database_upgrades_without_changing_existing_routes(tmp_path: Path) -> None:
    path = tmp_path / "upgrade-v3.db"
    database = SqliteDatabase(database_url(path))
    await database.open()
    try:
        await MigrationRunner(database, FORWARDER_MIGRATIONS[:3]).upgrade()
        await database.execute(
            """
            INSERT INTO forwarder_routes (
                id, source_chat_id, source_topic_id, destination_chat_id,
                destination_topic_id, mode, filter_json, enabled, fallback_to_copy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, -1001, None, -2001, None, "forward", "{}", 1, 1),
        )
        await MigrationRunner(database, FORWARDER_MIGRATIONS).upgrade()

        stored = await SqliteRouteRepository(database).list_all()
        assert stored == (Route(1, SourceEndpoint(-1001), DestinationEndpoint(-2001)),)
        assert await SqlitePollCursorRepository(database).get(-1001) is None
    finally:
        await database.close()
