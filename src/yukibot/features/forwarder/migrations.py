"""Database migrations owned exclusively by the forwarder feature."""

from yukibot.contracts import Migration

FORWARDER_MIGRATIONS = (
    Migration(
        scope="forwarder",
        version=1,
        description="create routes and message links",
        statements=(
            """
            CREATE TABLE forwarder_routes (
                id INTEGER PRIMARY KEY,
                source_chat_id INTEGER NOT NULL,
                source_topic_id INTEGER,
                destination_chat_id INTEGER NOT NULL,
                destination_topic_id INTEGER,
                mode TEXT NOT NULL CHECK (mode IN ('copy', 'forward')),
                filter_json TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                fallback_to_copy INTEGER NOT NULL CHECK (fallback_to_copy IN (0, 1))
            )
            """,
            """
            CREATE INDEX forwarder_routes_source_idx
            ON forwarder_routes (source_chat_id, enabled)
            """,
            """
            CREATE TABLE forwarder_message_links (
                route_id INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                destination_chat_id INTEGER NOT NULL,
                destination_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (route_id, source_chat_id, source_message_id),
                FOREIGN KEY (route_id) REFERENCES forwarder_routes(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX forwarder_links_source_message_idx
            ON forwarder_message_links (source_message_id)
            """,
        ),
    ),
    Migration(
        scope="forwarder",
        version=2,
        description="create durable forwarding jobs",
        statements=(
            """
            CREATE TABLE forwarder_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('receive', 'edit', 'delete')),
                deduplication_key TEXT NOT NULL UNIQUE,
                group_key TEXT,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'processing', 'succeeded', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                available_at REAL NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX forwarder_jobs_pending_idx
            ON forwarder_jobs (state, available_at, id)
            """,
            """
            CREATE INDEX forwarder_jobs_group_idx
            ON forwarder_jobs (group_key, state, available_at)
            """,
        ),
    ),
)

__all__ = ["FORWARDER_MIGRATIONS"]
