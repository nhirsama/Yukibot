import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yukibot.adapters.database import MigrationRunner, SqliteDatabase
from yukibot.features.summarizer.migrations import SUMMARIZER_MIGRATIONS
from yukibot.features.summarizer.models import (
    SummaryActionItem,
    SummaryDocument,
    SummaryEndpoint,
    SummaryModelConfig,
    SummaryRule,
    SummaryRuleDraft,
    SummaryRun,
    SummaryTopic,
)
from yukibot.features.summarizer.repository import SqliteSummaryRepository


async def test_summary_repository_persists_rules_runs_and_cascades_deletes(
    tmp_path: Path,
) -> None:
    database = SqliteDatabase(f"sqlite:///{tmp_path / 'summary.db'}")
    await database.open()
    await MigrationRunner(database, SUMMARIZER_MIGRATIONS).upgrade()
    repository = SqliteSummaryRepository(database)
    draft = SummaryRuleDraft(
        SummaryEndpoint(-1001, username="source"),
        SummaryEndpoint(-1002, topic_id=42, username="target"),
        21600,
    )
    document = SummaryDocument(
        (
            SummaryTopic(
                "Release",
                "Version one shipped.",
                (10, 11),
                ("Alice",),
                ("Publish",),
                (SummaryActionItem("Verify", "Bob", "Friday"),),
                ("Any regressions?",),
            ),
        )
    )
    try:
        model_config = SummaryModelConfig(
            "openai",
            "gpt-test",
            api_key="top-secret",
            base_url="https://models.example/v1",
            input_token_limit=16000,
            output_token_limit=2000,
        )
        await repository.save_model_config(model_config)
        stored_config = await repository.get_model_config()
        assert stored_config == model_config
        assert stored_config is not None
        assert "top-secret" not in repr(stored_config)

        configured = await repository.add_auto(draft)
        assert configured.id == 1
        assert await repository.list_all() == (configured,)

        disabled = replace(configured, enabled=False, window_seconds=3600)
        await repository.replace(disabled)
        assert await repository.list_all() == (disabled,)
        with pytest.raises(KeyError):
            await repository.replace(SummaryRule(99, SummaryEndpoint(-1), SummaryEndpoint(-2)))

        now = datetime.now(UTC)
        await repository.save(
            SummaryRun(
                disabled.id,
                now,
                now,
                10,
                11,
                2,
                "openai",
                "gpt-test",
                1,
                document,
            )
        )
        row = await database.fetch_one(
            "SELECT provider, model, output_json FROM summarizer_runs WHERE rule_id = ?",
            (disabled.id,),
        )
        assert row is not None
        assert row["provider"] == "openai"
        assert row["model"] == "gpt-test"
        payload = json.loads(str(row["output_json"]))
        assert payload["topics"][0]["action_items"][0]["owner"] == "Bob"

        assert await repository.remove(disabled.id)
        assert not await repository.remove(disabled.id)
        assert await database.fetch_all("SELECT id FROM summarizer_runs") == ()
        assert await repository.clear_model_config()
        assert await repository.get_model_config() is None
    finally:
        await database.close()
