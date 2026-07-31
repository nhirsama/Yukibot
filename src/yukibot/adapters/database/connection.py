"""Serialized aiosqlite implementation of the database contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

import aiosqlite

from yukibot.contracts.database import (
    DatabaseConnection,
    DatabaseError,
    ExecuteResult,
    IntegrityViolation,
    Row,
    SqlParameters,
)


def sqlite_path_from_url(url: str) -> str:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError("database URL must start with sqlite:///")
    path = url[len(prefix) :]
    if path == ":memory:":
        return path
    if not path:
        raise ValueError("database URL must contain a path")
    return f"/{path[1:]}" if path.startswith("/") else path


class _SqliteConnection(DatabaseConnection):
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, parameters: SqlParameters = ()) -> ExecuteResult:
        try:
            cursor = await self._connection.execute(sql, parameters)
        except aiosqlite.IntegrityError as error:
            raise IntegrityViolation(str(error)) from error
        except aiosqlite.Error as error:
            raise DatabaseError(str(error)) from error
        try:
            return ExecuteResult(cursor.rowcount, cursor.lastrowid)
        finally:
            await cursor.close()

    async def executemany(self, sql: str, parameters: Sequence[SqlParameters]) -> ExecuteResult:
        try:
            cursor = await self._connection.executemany(sql, parameters)
        except aiosqlite.IntegrityError as error:
            raise IntegrityViolation(str(error)) from error
        except aiosqlite.Error as error:
            raise DatabaseError(str(error)) from error
        try:
            return ExecuteResult(cursor.rowcount, cursor.lastrowid)
        finally:
            await cursor.close()

    async def fetch_one(self, sql: str, parameters: SqlParameters = ()) -> Row | None:
        try:
            cursor = await self._connection.execute(sql, parameters)
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        except aiosqlite.Error as error:
            raise DatabaseError(str(error)) from error
        return dict(row) if row is not None else None

    async def fetch_all(self, sql: str, parameters: SqlParameters = ()) -> Sequence[Row]:
        try:
            cursor = await self._connection.execute(sql, parameters)
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        except aiosqlite.Error as error:
            raise DatabaseError(str(error)) from error
        return tuple(dict(row) for row in rows)


class _Transaction:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def __aenter__(self) -> DatabaseConnection:
        await self._database._lock.acquire()
        try:
            connection = self._database._require_connection()
            await connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._database._lock.release()
            raise
        return _SqliteConnection(connection)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        connection = self._database._require_connection()
        try:
            if exc_type is None:
                await connection.commit()
            else:
                await connection.rollback()
        finally:
            self._database._lock.release()
        return False


class SqliteDatabase:
    def __init__(self, url: str) -> None:
        parsed_path = sqlite_path_from_url(url)
        self._path = (
            parsed_path if parsed_path == ":memory:" else str(Path(parsed_path).expanduser())
        )
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def open(self) -> None:
        if self._connection is not None:
            return
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path)
        connection.row_factory = aiosqlite.Row
        try:
            cursor = await connection.execute("PRAGMA foreign_keys = ON")
            await cursor.close()
            cursor = await connection.execute("PRAGMA busy_timeout = 5000")
            await cursor.close()
            if self._path != ":memory:":
                cursor = await connection.execute("PRAGMA journal_mode = WAL")
                await cursor.close()
            await connection.commit()
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    async def close(self) -> None:
        async with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                await connection.close()

    async def execute(self, sql: str, parameters: SqlParameters = ()) -> ExecuteResult:
        async with self._lock:
            connection = self._require_connection()
            result = await _SqliteConnection(connection).execute(sql, parameters)
            await connection.commit()
            return result

    async def executemany(self, sql: str, parameters: Sequence[SqlParameters]) -> ExecuteResult:
        async with self._lock:
            connection = self._require_connection()
            result = await _SqliteConnection(connection).executemany(sql, parameters)
            await connection.commit()
            return result

    async def fetch_one(self, sql: str, parameters: SqlParameters = ()) -> Row | None:
        async with self._lock:
            return await _SqliteConnection(self._require_connection()).fetch_one(sql, parameters)

    async def fetch_all(self, sql: str, parameters: SqlParameters = ()) -> Sequence[Row]:
        async with self._lock:
            return await _SqliteConnection(self._require_connection()).fetch_all(sql, parameters)

    async def ping(self) -> bool:
        try:
            row = await self.fetch_one("SELECT 1 AS alive")
        except DatabaseError:
            return False
        return row is not None and row.get("alive") == 1

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise DatabaseError("database is not open")
        return self._connection


@asynccontextmanager
async def opened_database(url: str) -> AsyncIterator[SqliteDatabase]:
    database = SqliteDatabase(url)
    await database.open()
    try:
        yield database
    finally:
        await database.close()
