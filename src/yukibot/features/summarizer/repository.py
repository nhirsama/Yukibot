"""SQLite persistence for summary rules and generated summaries."""

from __future__ import annotations

import json
from collections.abc import Sequence

from yukibot.contracts import Database, Row

from .models import (
    SummaryDocument,
    SummaryEndpoint,
    SummaryModelConfig,
    SummaryRule,
    SummaryRuleDraft,
    SummaryRun,
)

_RULE_COLUMNS = """
    id, source_chat_id, source_topic_id, source_username,
    destination_chat_id, destination_topic_id, destination_username,
    window_seconds, enabled
"""


class SqliteSummaryRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_all(self) -> Sequence[SummaryRule]:
        rows = await self._database.fetch_all(
            f"SELECT {_RULE_COLUMNS} FROM summarizer_rules ORDER BY id"
        )
        return tuple(_rule_from_row(row) for row in rows)

    async def add_auto(self, draft: SummaryRuleDraft) -> SummaryRule:
        result = await self._database.execute(
            """
            INSERT INTO summarizer_rules (
                source_chat_id, source_topic_id, source_username,
                destination_chat_id, destination_topic_id, destination_username,
                window_seconds, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.source.chat_id,
                draft.source.topic_id,
                draft.source.username,
                draft.destination.chat_id,
                draft.destination.topic_id,
                draft.destination.username,
                draft.window_seconds,
                int(draft.enabled),
            ),
        )
        if result.last_row_id is None or result.last_row_id <= 0:
            raise RuntimeError("database did not allocate a summary rule ID")
        return draft.bind(result.last_row_id)

    async def replace(self, rule: SummaryRule) -> None:
        result = await self._database.execute(
            """
            UPDATE summarizer_rules SET
                source_chat_id = ?, source_topic_id = ?, source_username = ?,
                destination_chat_id = ?, destination_topic_id = ?,
                destination_username = ?, window_seconds = ?, enabled = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                rule.source.chat_id,
                rule.source.topic_id,
                rule.source.username,
                rule.destination.chat_id,
                rule.destination.topic_id,
                rule.destination.username,
                rule.window_seconds,
                int(rule.enabled),
                rule.id,
            ),
        )
        if result.row_count == 0:
            raise KeyError(rule.id)

    async def remove(self, rule_id: int) -> bool:
        result = await self._database.execute(
            "DELETE FROM summarizer_rules WHERE id = ?", (rule_id,)
        )
        return result.row_count > 0

    async def save(self, run: SummaryRun) -> None:
        await self._database.execute(
            """
            INSERT INTO summarizer_runs (
                rule_id, started_at, completed_at, first_message_id,
                last_message_id, message_count, provider, model,
                prompt_version, output_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.rule_id,
                run.started_at.isoformat(),
                run.completed_at.isoformat(),
                run.first_message_id,
                run.last_message_id,
                run.message_count,
                run.provider,
                run.model,
                run.prompt_version,
                _document_json(run.document),
            ),
        )

    async def get_model_config(self) -> SummaryModelConfig | None:
        row = await self._database.fetch_one(
            """
            SELECT provider, model, api_key, base_url, input_token_limit,
                   output_token_limit, temperature, timeout, max_retries
            FROM summarizer_model_config WHERE id = 1
            """
        )
        if row is None:
            return None
        return SummaryModelConfig(
            provider=_str(row, "provider"),
            model=_str(row, "model"),
            api_key=_optional_str(row, "api_key"),
            base_url=_optional_str(row, "base_url"),
            input_token_limit=_int(row, "input_token_limit"),
            output_token_limit=_int(row, "output_token_limit"),
            temperature=_float(row, "temperature"),
            timeout=_float(row, "timeout"),
            max_retries=_int(row, "max_retries"),
        )

    async def save_model_config(self, config: SummaryModelConfig) -> None:
        await self._database.execute(
            """
            INSERT INTO summarizer_model_config (
                id, provider, model, api_key, base_url, input_token_limit,
                output_token_limit, temperature, timeout, max_retries
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                provider = excluded.provider,
                model = excluded.model,
                api_key = excluded.api_key,
                base_url = excluded.base_url,
                input_token_limit = excluded.input_token_limit,
                output_token_limit = excluded.output_token_limit,
                temperature = excluded.temperature,
                timeout = excluded.timeout,
                max_retries = excluded.max_retries,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                config.provider,
                config.model,
                config.api_key,
                config.base_url,
                config.input_token_limit,
                config.output_token_limit,
                config.temperature,
                config.timeout,
                config.max_retries,
            ),
        )

    async def clear_model_config(self) -> bool:
        result = await self._database.execute("DELETE FROM summarizer_model_config WHERE id = 1")
        return result.row_count > 0


def _rule_from_row(row: Row) -> SummaryRule:
    return SummaryRule(
        _int(row, "id"),
        SummaryEndpoint(
            _int(row, "source_chat_id"),
            _optional_int(row, "source_topic_id"),
            _optional_str(row, "source_username"),
        ),
        SummaryEndpoint(
            _int(row, "destination_chat_id"),
            _optional_int(row, "destination_topic_id"),
            _optional_str(row, "destination_username"),
        ),
        _int(row, "window_seconds"),
        bool(_int(row, "enabled")),
    )


def _document_json(document: SummaryDocument) -> str:
    return json.dumps(
        {
            "topics": [
                {
                    "title": topic.title,
                    "summary": topic.summary,
                    "evidence_message_ids": topic.evidence_message_ids,
                    "participants": topic.participants,
                    "decisions": topic.decisions,
                    "action_items": [
                        {
                            "task": item.task,
                            "owner": item.owner,
                            "deadline": item.deadline,
                        }
                        for item in topic.action_items
                    ],
                    "open_questions": topic.open_questions,
                }
                for topic in document.topics
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _int(row: Row, name: str) -> int:
    value = row[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"column {name} is not an integer")
    return value


def _optional_int(row: Row, name: str) -> int | None:
    value = row[name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"column {name} is not an integer")
    return value


def _optional_str(row: Row, name: str) -> str | None:
    value = row[name]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"column {name} is not text")
    return value


def _str(row: Row, name: str) -> str:
    value = row[name]
    if not isinstance(value, str):
        raise TypeError(f"column {name} is not text")
    return value


def _float(row: Row, name: str) -> float:
    value = row[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"column {name} is not numeric")
    return float(value)
