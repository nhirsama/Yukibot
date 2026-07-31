"""Lifecycle component that opens, migrates and closes the database."""

from __future__ import annotations

from collections.abc import Iterable

from yukibot.contracts.database import Database, Migration

from .migrations import MigrationRunner


class DatabaseLifecycle:
    name = "database"

    def __init__(self, database: Database, migrations: Iterable[Migration]) -> None:
        self._database = database
        self._runner = MigrationRunner(database, migrations)

    async def start(self) -> None:
        await self._database.open()
        try:
            await self._runner.upgrade()
        except BaseException:
            await self._database.close()
            raise

    async def stop(self) -> None:
        await self._database.close()
