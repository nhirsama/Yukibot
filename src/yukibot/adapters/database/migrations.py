"""Feature-scoped, checksum-protected forward-only migrations."""

from __future__ import annotations

from collections.abc import Iterable

from yukibot.contracts.database import Database, DatabaseConnection, Migration, Row


class MigrationDriftError(RuntimeError):
    """An applied migration no longer matches its registered SQL."""


class MigrationRunner:
    def __init__(self, database: Database, migrations: Iterable[Migration]) -> None:
        self._database = database
        self._migrations = tuple(sorted(migrations, key=lambda item: (item.scope, item.version)))
        keys = [(migration.scope, migration.version) for migration in self._migrations]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate migration scope and version")

    async def upgrade(self) -> tuple[tuple[str, int], ...]:
        await self._ensure_table()
        applied_rows = await self._database.fetch_all(
            "SELECT scope, version, checksum FROM yukibot_schema_migrations"
        )
        applied = {
            (_required_str(row, "scope"), _required_int(row, "version")): _required_str(
                row, "checksum"
            )
            for row in applied_rows
        }
        completed: list[tuple[str, int]] = []
        for migration in self._migrations:
            key = (migration.scope, migration.version)
            recorded = applied.get(key)
            if recorded is not None:
                if recorded != migration.checksum:
                    raise MigrationDriftError(
                        f"applied migration {migration.scope}:{migration.version} changed"
                    )
                continue
            async with self._database.transaction() as transaction:
                await self._apply(transaction, migration)
            completed.append(key)
        return tuple(completed)

    async def _ensure_table(self) -> None:
        await self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS yukibot_schema_migrations (
                scope TEXT NOT NULL,
                version INTEGER NOT NULL,
                description TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (scope, version)
            )
            """
        )

    @staticmethod
    async def _apply(transaction: DatabaseConnection, migration: Migration) -> None:
        for statement in migration.statements:
            await transaction.execute(statement)
        await transaction.execute(
            """
            INSERT INTO yukibot_schema_migrations (scope, version, description, checksum)
            VALUES (?, ?, ?, ?)
            """,
            (migration.scope, migration.version, migration.description, migration.checksum),
        )


def _required_str(row: Row, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise TypeError(f"database column {key!r} is not a string")
    return value


def _required_int(row: Row, key: str) -> int:
    value = row.get(key)
    if not isinstance(value, int):
        raise TypeError(f"database column {key!r} is not an integer")
    return value
