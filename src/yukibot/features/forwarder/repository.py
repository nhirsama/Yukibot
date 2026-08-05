"""Forwarder-owned repositories built on the generic database contract."""

from __future__ import annotations

import json
from collections.abc import Sequence

from yukibot.contracts.database import Database, DatabaseConnection, Row

from .models import (
    ContentType,
    DestinationEndpoint,
    ForwardMode,
    ManagedTopic,
    MessageFilter,
    MessageLink,
    MessageRef,
    PollCursor,
    Route,
    RouteDraft,
    SourceEndpoint,
)
from .recovery import ChatAccess
from .routing import assert_acyclic_routes

_ROUTE_COLUMNS = """
    id, source_chat_id, source_topic_id, destination_chat_id,
    destination_topic_id, mode, filter_json, enabled, fallback_to_copy,
    source_username, destination_username, poll_interval_seconds
"""


class SqliteRouteRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_for_source_chat(self, chat_id: int) -> Sequence[Route]:
        rows = await self._database.fetch_all(
            f"SELECT {_ROUTE_COLUMNS} FROM forwarder_routes WHERE source_chat_id = ? ORDER BY id",
            (chat_id,),
        )
        return tuple(_route_from_row(row) for row in rows)

    async def list_all(self) -> Sequence[Route]:
        return await _list_routes(self._database)

    async def add(self, route: Route) -> None:
        async with self._database.transaction() as transaction:
            routes = await _list_routes(transaction)
            if any(item.id == route.id for item in routes):
                raise ValueError(f"route {route.id} already exists")
            assert_acyclic_routes((*routes, route))
            await transaction.execute(
                """
                INSERT INTO forwarder_routes (
                    id, source_chat_id, source_topic_id, destination_chat_id,
                    destination_topic_id, mode, filter_json, enabled, fallback_to_copy,
                    source_username, destination_username, poll_interval_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _route_parameters(route),
            )
            await _ensure_route_access(transaction, route)

    async def add_auto(self, draft: RouteDraft) -> Route:
        async with self._database.transaction() as transaction:
            routes = await _list_routes(transaction)
            provisional = draft.bind(max((route.id for route in routes), default=0) + 1)
            assert_acyclic_routes((*routes, provisional))
            result = await transaction.execute(
                """
                INSERT INTO forwarder_routes (
                    source_chat_id, source_topic_id, destination_chat_id,
                    destination_topic_id, mode, filter_json, enabled, fallback_to_copy,
                    source_username, destination_username, poll_interval_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _route_parameters(provisional)[1:],
            )
            if result.last_row_id is None or result.last_row_id <= 0:
                raise RuntimeError("database did not allocate a forwarding route ID")
            route = draft.bind(result.last_row_id)
            await _ensure_route_access(transaction, route)
            return route

    async def replace(self, route: Route) -> None:
        async with self._database.transaction() as transaction:
            routes = await _list_routes(transaction)
            if not any(item.id == route.id for item in routes):
                raise KeyError(route.id)
            existing = next(item for item in routes if item.id == route.id)
            proposed = tuple(route if item.id == route.id else item for item in routes)
            assert_acyclic_routes(proposed)
            await transaction.execute(
                """
                UPDATE forwarder_routes SET
                    source_chat_id = ?, source_topic_id = ?, destination_chat_id = ?,
                    destination_topic_id = ?, mode = ?, filter_json = ?, enabled = ?,
                    fallback_to_copy = ?, source_username = ?, destination_username = ?,
                    poll_interval_seconds = ?
                WHERE id = ?
                """,
                (*_route_parameters(route)[1:], route.id),
            )
            await _ensure_route_access(transaction, route)
            await _remove_unreferenced_access(
                transaction,
                {
                    existing.source.chat_id,
                    existing.destination.chat_id,
                }
                - {route.source.chat_id, route.destination.chat_id},
            )

    async def remove(self, route_id: int) -> bool:
        async with self._database.transaction() as transaction:
            row = await transaction.fetch_one(
                """
                SELECT source_chat_id, destination_chat_id
                FROM forwarder_routes
                WHERE id = ?
                """,
                (route_id,),
            )
            if row is None:
                return False
            result = await transaction.execute(
                "DELETE FROM forwarder_routes WHERE id = ?", (route_id,)
            )
            await _remove_unreferenced_access(
                transaction,
                {
                    _int_column(row, "source_chat_id"),
                    _int_column(row, "destination_chat_id"),
                },
            )
            return result.row_count > 0


class SqliteMessageLinkRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def save_many(self, links: Sequence[MessageLink]) -> None:
        if not links:
            return
        await self._database.executemany(
            """
            INSERT INTO forwarder_message_links (
                route_id, source_chat_id, source_message_id,
                destination_chat_id, destination_message_id, delivery_mode
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (route_id, source_chat_id, source_message_id) DO UPDATE SET
                destination_chat_id = excluded.destination_chat_id,
                destination_message_id = excluded.destination_message_id,
                delivery_mode = excluded.delivery_mode
            """,
            tuple(
                (
                    link.route_id,
                    link.source.chat_id,
                    link.source.message_id,
                    link.destination.chat_id,
                    link.destination.message_id,
                    link.delivery_mode.value,
                )
                for link in links
            ),
        )

    async def get(self, route_id: int, source: MessageRef) -> MessageLink | None:
        row = await self._database.fetch_one(
            """
            SELECT route_id, source_chat_id, source_message_id,
                   destination_chat_id, destination_message_id, delivery_mode
            FROM forwarder_message_links
            WHERE route_id = ? AND source_chat_id = ? AND source_message_id = ?
            """,
            (route_id, source.chat_id, source.message_id),
        )
        return _link_from_row(row) if row is not None else None

    async def find_all(self, source: MessageRef) -> Sequence[MessageLink]:
        rows = await self._database.fetch_all(
            """
            SELECT route_id, source_chat_id, source_message_id,
                   destination_chat_id, destination_message_id, delivery_mode
            FROM forwarder_message_links
            WHERE source_chat_id = ? AND source_message_id = ?
            ORDER BY route_id
            """,
            (source.chat_id, source.message_id),
        )
        return tuple(_link_from_row(row) for row in rows)

    async def find_by_source_message_id(self, message_id: int) -> Sequence[MessageLink]:
        rows = await self._database.fetch_all(
            """
            SELECT route_id, source_chat_id, source_message_id,
                   destination_chat_id, destination_message_id, delivery_mode
            FROM forwarder_message_links
            WHERE source_message_id = ?
            ORDER BY route_id, source_chat_id
            """,
            (message_id,),
        )
        return tuple(_link_from_row(row) for row in rows)

    async def remove(self, link: MessageLink) -> None:
        await self._database.execute(
            """
            DELETE FROM forwarder_message_links
            WHERE route_id = ? AND source_chat_id = ? AND source_message_id = ?
            """,
            (link.route_id, link.source.chat_id, link.source.message_id),
        )


class SqliteManagedTopicRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, source_chat_id: int, destination_chat_id: int) -> ManagedTopic | None:
        row = await self._database.fetch_one(
            """
            SELECT source_chat_id, destination_chat_id, topic_id, title
            FROM forwarder_managed_topics
            WHERE source_chat_id = ? AND destination_chat_id = ?
            """,
            (source_chat_id, destination_chat_id),
        )
        return _managed_topic_from_row(row) if row is not None else None

    async def save(self, topic: ManagedTopic) -> None:
        await self._database.execute(
            """
            INSERT INTO forwarder_managed_topics (
                source_chat_id, destination_chat_id, topic_id, title
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (source_chat_id, destination_chat_id) DO UPDATE SET
                topic_id = excluded.topic_id,
                title = excluded.title,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                topic.source_chat_id,
                topic.destination_chat_id,
                topic.topic_id,
                topic.title,
            ),
        )


class SqlitePollCursorRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, source_chat_id: int) -> PollCursor | None:
        row = await self._database.fetch_one(
            """
            SELECT source_chat_id, last_message_id
            FROM forwarder_poll_cursors
            WHERE source_chat_id = ?
            """,
            (source_chat_id,),
        )
        if row is None:
            return None
        return PollCursor(
            source_chat_id=_int_column(row, "source_chat_id"),
            last_message_id=_int_column(row, "last_message_id"),
        )

    async def save(self, cursor: PollCursor) -> None:
        await self._database.execute(
            """
            INSERT INTO forwarder_poll_cursors (source_chat_id, last_message_id)
            VALUES (?, ?)
            ON CONFLICT (source_chat_id) DO UPDATE SET
                last_message_id = max(last_message_id, excluded.last_message_id),
                updated_at = CURRENT_TIMESTAMP
            """,
            (cursor.source_chat_id, cursor.last_message_id),
        )


class SqliteChatAccessRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_many(self, chat_ids: Sequence[int]) -> Sequence[ChatAccess]:
        if not chat_ids:
            return ()
        placeholders = ", ".join("?" for _ in chat_ids)
        rows = await self._database.fetch_all(
            f"""
            SELECT chat_id, title, username, invite_link
            FROM forwarder_chat_access
            WHERE chat_id IN ({placeholders})
            ORDER BY chat_id
            """,
            tuple(chat_ids),
        )
        return tuple(_chat_access_from_row(row) for row in rows)

    async def save(self, access: ChatAccess) -> None:
        await self._database.execute(
            """
            INSERT INTO forwarder_chat_access (chat_id, title, username, invite_link)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (chat_id) DO UPDATE SET
                title = coalesce(excluded.title, title),
                username = excluded.username,
                invite_link = excluded.invite_link,
                updated_at = CURRENT_TIMESTAMP
            """,
            (access.chat_id, access.title, access.username, access.invite_link),
        )


async def _list_routes(connection: DatabaseConnection) -> tuple[Route, ...]:
    rows = await connection.fetch_all(f"SELECT {_ROUTE_COLUMNS} FROM forwarder_routes ORDER BY id")
    return tuple(_route_from_row(row) for row in rows)


async def _ensure_route_access(connection: DatabaseConnection, route: Route) -> None:
    for chat_id, username in (
        (route.source.chat_id, route.source.username),
        (route.destination.chat_id, route.destination.username),
    ):
        await connection.execute(
            """
            INSERT INTO forwarder_chat_access (chat_id, username, invite_link)
            VALUES (?, ?, ?)
            ON CONFLICT (chat_id) DO NOTHING
            """,
            (
                chat_id,
                username,
                f"https://t.me/{username}" if username is not None else None,
            ),
        )


async def _remove_unreferenced_access(
    connection: DatabaseConnection,
    chat_ids: set[int],
) -> None:
    for chat_id in chat_ids:
        await connection.execute(
            """
            DELETE FROM forwarder_chat_access
            WHERE chat_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM forwarder_routes
                  WHERE source_chat_id = ? OR destination_chat_id = ?
              )
            """,
            (chat_id, chat_id, chat_id),
        )


def _route_parameters(route: Route) -> tuple[str | int | None, ...]:
    message_filter = {
        "keywords": list(route.message_filter.keywords),
        "allowed_content_types": sorted(route.message_filter.allowed_content_types),
        "blocked_content_types": sorted(route.message_filter.blocked_content_types),
        "include_service_messages": route.message_filter.include_service_messages,
    }
    return (
        route.id,
        route.source.chat_id,
        route.source.topic_id,
        route.destination.chat_id,
        route.destination.topic_id,
        route.mode.value,
        json.dumps(message_filter, separators=(",", ":"), sort_keys=True),
        int(route.enabled),
        int(route.fallback_to_copy),
        route.source.username,
        route.destination.username,
        route.source.poll_interval_seconds,
    )


def _route_from_row(row: Row) -> Route:
    raw_filter = json.loads(_str_column(row, "filter_json"))
    if not isinstance(raw_filter, dict):
        raise TypeError("filter_json must contain an object")
    allowed = _content_types(raw_filter.get("allowed_content_types", []))
    blocked = _content_types(raw_filter.get("blocked_content_types", []))
    keywords = _strings(raw_filter.get("keywords", []))
    include_service = raw_filter.get("include_service_messages", False)
    if not isinstance(include_service, bool):
        raise TypeError("include_service_messages must be a boolean")
    return Route(
        id=_int_column(row, "id"),
        source=SourceEndpoint(
            _int_column(row, "source_chat_id"),
            _optional_int_column(row, "source_topic_id"),
            username=_optional_str_column(row, "source_username"),
            poll_interval_seconds=_optional_int_column(row, "poll_interval_seconds"),
        ),
        destination=DestinationEndpoint(
            _int_column(row, "destination_chat_id"),
            _optional_int_column(row, "destination_topic_id"),
            username=_optional_str_column(row, "destination_username"),
        ),
        mode=ForwardMode(_str_column(row, "mode")),
        message_filter=MessageFilter(
            keywords=keywords,
            allowed_content_types=frozenset(allowed),
            blocked_content_types=frozenset(blocked),
            include_service_messages=include_service,
        ),
        enabled=_bool_column(row, "enabled"),
        fallback_to_copy=_bool_column(row, "fallback_to_copy"),
    )


def _link_from_row(row: Row) -> MessageLink:
    return MessageLink(
        route_id=_int_column(row, "route_id"),
        source=MessageRef(
            _int_column(row, "source_chat_id"), _int_column(row, "source_message_id")
        ),
        destination=MessageRef(
            _int_column(row, "destination_chat_id"),
            _int_column(row, "destination_message_id"),
        ),
        delivery_mode=ForwardMode(_str_column(row, "delivery_mode")),
    )


def _managed_topic_from_row(row: Row) -> ManagedTopic:
    return ManagedTopic(
        source_chat_id=_int_column(row, "source_chat_id"),
        destination_chat_id=_int_column(row, "destination_chat_id"),
        topic_id=_int_column(row, "topic_id"),
        title=_str_column(row, "title"),
    )


def _chat_access_from_row(row: Row) -> ChatAccess:
    return ChatAccess(
        chat_id=_int_column(row, "chat_id"),
        title=_optional_str_column(row, "title"),
        username=_optional_str_column(row, "username"),
        invite_link=_optional_str_column(row, "invite_link"),
    )


def _int_column(row: Row, key: str) -> int:
    value = row.get(key)
    if not isinstance(value, int):
        raise TypeError(f"database column {key!r} is not an integer")
    return value


def _optional_int_column(row: Row, key: str) -> int | None:
    value = row.get(key)
    if value is not None and not isinstance(value, int):
        raise TypeError(f"database column {key!r} is not an integer or null")
    return value


def _str_column(row: Row, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise TypeError(f"database column {key!r} is not a string")
    return value


def _optional_str_column(row: Row, key: str) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"database column {key!r} is not a string or null")
    return value


def _bool_column(row: Row, key: str) -> bool:
    value = _int_column(row, key)
    if value not in (0, 1):
        raise ValueError(f"database column {key!r} must be zero or one")
    return bool(value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError("expected a list of strings")
    return tuple(value)


def _content_types(value: object) -> tuple[ContentType, ...]:
    return tuple(ContentType(item) for item in _strings(value))
