from pathlib import Path

import pytest

from yukibot.adapters.database import SqliteDatabase, sqlite_path_from_url
from yukibot.contracts import DatabaseError, IntegrityViolation


def database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def test_sqlite_url_parsing() -> None:
    assert sqlite_path_from_url("sqlite:///:memory:") == ":memory:"
    assert sqlite_path_from_url("sqlite:///data/app.db") == "data/app.db"
    assert sqlite_path_from_url("sqlite:////tmp/app.db") == "/tmp/app.db"
    with pytest.raises(ValueError, match="must start"):
        sqlite_path_from_url("postgresql:///app")


async def test_open_execute_fetch_and_ping(tmp_path: Path) -> None:
    database = SqliteDatabase(database_url(tmp_path / "nested" / "app.db"))
    await database.open()
    await database.open()
    try:
        assert await database.ping()
        await database.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
        )
        inserted = await database.execute("INSERT INTO items (name) VALUES (?)", ("one",))
        row = await database.fetch_one("SELECT id, name FROM items WHERE id = ?", (1,))

        assert inserted.last_row_id == 1
        assert inserted.row_count == 1
        assert row == {"id": 1, "name": "one"}
        assert await database.fetch_all("SELECT name FROM items") == ({"name": "one"},)

        with pytest.raises(IntegrityViolation):
            await database.execute("INSERT INTO items (name) VALUES (?)", ("one",))
    finally:
        await database.close()
        await database.close()

    assert not await database.ping()
    with pytest.raises(DatabaseError, match="not open"):
        await database.fetch_all("SELECT 1")


async def test_transaction_commits_or_rolls_back(tmp_path: Path) -> None:
    async with SqliteDatabase(database_url(tmp_path / "tx.db")) as database:
        await database.execute("CREATE TABLE items (value TEXT NOT NULL)")

        async with database.transaction() as transaction:
            await transaction.execute("INSERT INTO items VALUES (?)", ("committed",))

        with pytest.raises(RuntimeError, match="abort"):
            async with database.transaction() as transaction:
                await transaction.execute("INSERT INTO items VALUES (?)", ("rolled-back",))
                raise RuntimeError("abort")

        rows = await database.fetch_all("SELECT value FROM items ORDER BY rowid")
        assert rows == ({"value": "committed"},)
