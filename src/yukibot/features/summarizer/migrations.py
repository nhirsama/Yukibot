"""Database migrations owned exclusively by the summarizer feature."""

from yukibot.contracts import Migration

SUMMARIZER_MIGRATIONS = (
    Migration(
        scope="summarizer",
        version=1,
        description="create summary rules and successful run history",
        statements=(
            """
            CREATE TABLE summarizer_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                source_topic_id INTEGER,
                source_username TEXT,
                destination_chat_id INTEGER NOT NULL,
                destination_topic_id INTEGER,
                destination_username TEXT,
                window_seconds INTEGER NOT NULL
                    CHECK (window_seconds BETWEEN 60 AND 2592000),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX summarizer_rules_source_idx
            ON summarizer_rules (source_chat_id, source_topic_id, enabled)
            """,
            """
            CREATE TABLE summarizer_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                first_message_id INTEGER NOT NULL CHECK (first_message_id > 0),
                last_message_id INTEGER NOT NULL CHECK (last_message_id >= first_message_id),
                message_count INTEGER NOT NULL CHECK (message_count > 0),
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version INTEGER NOT NULL CHECK (prompt_version > 0),
                output_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (rule_id) REFERENCES summarizer_rules(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX summarizer_runs_rule_idx
            ON summarizer_runs (rule_id, id DESC)
            """,
        ),
    ),
    Migration(
        scope="summarizer",
        version=2,
        description="create command-managed summary model configuration",
        statements=(
            """
            CREATE TABLE summarizer_model_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key TEXT,
                base_url TEXT,
                input_token_limit INTEGER NOT NULL
                    CHECK (input_token_limit > output_token_limit + 2000),
                output_token_limit INTEGER NOT NULL CHECK (output_token_limit > 0),
                temperature REAL NOT NULL CHECK (temperature BETWEEN 0 AND 2),
                timeout REAL NOT NULL CHECK (timeout > 0 AND timeout <= 1800),
                max_retries INTEGER NOT NULL CHECK (max_retries BETWEEN 0 AND 10),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
    ),
)

__all__ = ["SUMMARIZER_MIGRATIONS"]
