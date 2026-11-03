"""Image smoke check: load the sync driver; optionally check both DB connections.

Run from /app: python scripts/check_database.py [--connect]
Credentials are never included in diagnostic output, including on failure.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from nordpool_predictor.config import get_settings


async def check(connect: bool) -> None:
    settings = get_settings()
    sync_engine = create_engine(settings.sync_database_url)
    try:
        if sync_engine.dialect.driver != "psycopg2":
            raise RuntimeError("Unexpected synchronous driver")
        if connect:
            with sync_engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                if connection.execute(text("SELECT 1")).scalar_one() != 1:
                    raise RuntimeError("Synchronous query failed")
    finally:
        sync_engine.dispose()
    if connect:
        async_engine = create_async_engine(settings.database_url)
        try:
            async with async_engine.connect() as connection:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                if (await connection.execute(text("SELECT 1"))).scalar_one() != 1:
                    raise RuntimeError("Asynchronous query failed")
        finally:
            await async_engine.dispose()
    print(
        "Sync psycopg2 driver loaded; "
        + ("both DB connections passed" if connect else "no DB access")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(check(args.connect))
    except Exception as exc:
        raise SystemExit(
            f"Database check failed ({type(exc).__name__}); credentials omitted"
        ) from None
