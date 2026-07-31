from pathlib import Path

import pytest

from yukibot.adapters.database import MigrationDriftError, MigrationRunner, SqliteDatabase
from yukibot.contracts import DatabaseError, Migration


def database_url(path: Path) -> str:
    return f"sqlite:///{path}"


async def test_migrations_are_ordered_and_idempotent(tmp_path: Path) -> None:
    database = SqliteDatabase(database_url(tmp_path / "migrations.db"))
    await database.open()
    migrations = (
        Migration("feature_b", 1, "b", ("CREATE TABLE b (id INTEGER)",)),
        Migration("feature_a", 2, "a2", ("ALTER TABLE a ADD COLUMN name TEXT",)),
        Migration("feature_a", 1, "a1", ("CREATE TABLE a (id INTEGER)",)),
    )
    runner = MigrationRunner(database, migrations)
    try:
        assert await runner.upgrade() == (
            ("feature_a", 1),
            ("feature_a", 2),
            ("feature_b", 1),
        )
        assert await runner.upgrade() == ()
        rows = await database.fetch_all(
            "SELECT scope, version FROM yukibot_schema_migrations ORDER BY scope, version"
        )
        assert rows == (
            {"scope": "feature_a", "version": 1},
            {"scope": "feature_a", "version": 2},
            {"scope": "feature_b", "version": 1},
        )
    finally:
        await database.close()


async def test_changed_applied_migration_is_rejected(tmp_path: Path) -> None:
    async with SqliteDatabase(database_url(tmp_path / "drift.db")) as database:
        original = Migration("archive", 1, "create", ("CREATE TABLE archive (id INTEGER)",))
        await MigrationRunner(database, (original,)).upgrade()
        changed = Migration(
            "archive", 1, "create", ("CREATE TABLE archive (id INTEGER, text TEXT)",)
        )

        with pytest.raises(MigrationDriftError, match="changed"):
            await MigrationRunner(database, (changed,)).upgrade()


async def test_failed_migration_is_rolled_back_and_not_recorded(tmp_path: Path) -> None:
    async with SqliteDatabase(database_url(tmp_path / "failure.db")) as database:
        broken = Migration(
            "broken",
            1,
            "broken migration",
            ("CREATE TABLE temporary_table (id INTEGER)", "NOT VALID SQL"),
        )
        with pytest.raises(DatabaseError):
            await MigrationRunner(database, (broken,)).upgrade()

        applied = await database.fetch_all(
            "SELECT scope FROM yukibot_schema_migrations WHERE scope = 'broken'"
        )
        table = await database.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'temporary_table'"
        )
        assert applied == ()
        assert table is None


def test_migration_definition_validation() -> None:
    with pytest.raises(ValueError, match="scope"):
        Migration("bad-scope", 1, "bad", ("SELECT 1",))
    with pytest.raises(ValueError, match="positive"):
        Migration("scope", 0, "bad", ("SELECT 1",))
    with pytest.raises(ValueError, match="statements"):
        Migration("scope", 1, "bad", ())
    with pytest.raises(ValueError, match="duplicate"):
        MigrationRunner(
            SqliteDatabase("sqlite:///:memory:"),
            (
                Migration("scope", 1, "one", ("SELECT 1",)),
                Migration("scope", 1, "two", ("SELECT 2",)),
            ),
        )
