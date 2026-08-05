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
    Migration(
        scope="forwarder",
        version=3,
        description="store automatically managed forum topics",
        statements=(
            """
            CREATE TABLE forwarder_managed_topics (
                source_chat_id INTEGER NOT NULL,
                destination_chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL CHECK (topic_id > 0),
                title TEXT NOT NULL CHECK (length(title) > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_chat_id, destination_chat_id),
                UNIQUE (destination_chat_id, topic_id)
            )
            """,
        ),
    ),
    Migration(
        scope="forwarder",
        version=4,
        description="store public chat references and polling cursors",
        statements=(
            """
            ALTER TABLE forwarder_routes ADD COLUMN source_username TEXT
            """,
            """
            ALTER TABLE forwarder_routes ADD COLUMN destination_username TEXT
            """,
            """
            ALTER TABLE forwarder_routes ADD COLUMN poll_interval_seconds INTEGER
                CHECK (poll_interval_seconds IS NULL OR poll_interval_seconds >= 60)
            """,
            """
            CREATE TABLE forwarder_poll_cursors (
                source_chat_id INTEGER PRIMARY KEY,
                last_message_id INTEGER NOT NULL CHECK (last_message_id >= 0),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
    ),
)

__all__ = ["FORWARDER_MIGRATIONS"]
