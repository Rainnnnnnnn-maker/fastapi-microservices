"""Keep persistence behind a small interface shared by D1 and local SQLite."""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

from ems.common import Reading

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS readings (
    site_id TEXT NOT NULL,
    interval_start TEXT NOT NULL,
    energy_kwh REAL NOT NULL CHECK (energy_kwh >= 0 AND energy_kwh <= 1000000),
    PRIMARY KEY (site_id, interval_start)
);
"""
INSERT_READING = """
INSERT INTO readings (site_id, interval_start, energy_kwh) VALUES (?1, ?2, ?3)
ON CONFLICT(site_id, interval_start) DO NOTHING
"""
SELECT_READING = """
SELECT site_id, interval_start, energy_kwh FROM readings
WHERE site_id = ?1 AND interval_start = ?2
"""
SELECT_LATEST = """
SELECT site_id, interval_start, energy_kwh FROM readings
WHERE site_id = ?1 ORDER BY interval_start DESC LIMIT 1
"""


class ConflictError(Exception):
    """A measurement already exists with different energy for this interval."""


class ReadingRepository(Protocol):
    async def save(self, reading: Reading) -> None: ...

    async def latest(self, site_id: str) -> Reading | None: ...


def _parameters(reading: Reading) -> tuple[str, str, float]:
    # UTC is validated by Reading, so ISO text also sorts chronologically.
    return reading.site_id, reading.interval_start.isoformat(), reading.energy_kwh


def _to_reading(row: Any) -> Reading | None:
    if row is None:
        return None
    # The Workers SDK may unwrap D1 rows; raw JS proxies need conversion.
    values = row.to_py() if hasattr(row, "to_py") else dict(row)
    return Reading.model_validate(values)


def _check_existing(existing: Reading | None, reading: Reading) -> None:
    if existing is None:
        raise RuntimeError("Inserted reading could not be retrieved")
    if existing.energy_kwh != reading.energy_kwh:
        raise ConflictError("A different reading already exists for this interval")


class D1ReadingRepository:
    """Use the Worker DB binding; never fall back to process memory or a file."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def save(self, reading: Reading) -> None:
        parameters = _parameters(reading)
        await self._db.prepare(INSERT_READING).bind(*parameters).run()
        row = await self._db.prepare(SELECT_READING).bind(*parameters[:2]).first()
        _check_existing(_to_reading(row), reading)

    async def latest(self, site_id: str) -> Reading | None:
        row = await self._db.prepare(SELECT_LATEST).bind(site_id).first()
        return _to_reading(row)


class SQLiteReadingRepository:
    """Local CPython adapter, explicitly constructed by the development runner."""

    def __init__(self, path: str | Path) -> None:
        # sqlite3 is unavailable on Workers; import only inside the local adapter.
        import sqlite3

        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path)) as connection:
            connection.executescript(CREATE_TABLE)

    async def save(self, reading: Reading) -> None:
        await asyncio.to_thread(self._save, reading)

    def _save(self, reading: Reading) -> None:
        import sqlite3

        parameters = _parameters(reading)
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute(INSERT_READING, parameters)
            row = connection.execute(SELECT_READING, parameters[:2]).fetchone()
            _check_existing(_to_reading(row), reading)

    async def latest(self, site_id: str) -> Reading | None:
        return await asyncio.to_thread(self._latest, site_id)

    def _latest(self, site_id: str) -> Reading | None:
        import sqlite3

        with closing(sqlite3.connect(self._path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(SELECT_LATEST, (site_id,)).fetchone()
            return _to_reading(row)
