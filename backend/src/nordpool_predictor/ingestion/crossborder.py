from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from nordpool_predictor.database import get_session
from nordpool_predictor.ingestion.eds_client import eds_get

logger = logging.getLogger(__name__)

DATASET = "GenerationProdTypeExchange"
SOURCE = "energidataservice"

EXCHANGE_COLUMNS = {
    "DK1": {
        "ExchangeGermany": "DK1-DE",
        "ExchangeNorway": "DK1-NO2",
        "ExchangeSweden": "DK1-SE3",
        "ExchangeGreatBelt": "DK1-DK2",
    },
    "DK2": {
        "ExchangeSweden": "DK2-SE4",
    },
}

ALL_COLUMNS = (
    "TimeUTC,PriceArea,"
    "ExchangeGreatBelt,ExchangeGermany,ExchangeSweden,"
    "ExchangeNorway,ExchangeNetherlands,ExchangeGreatBritain"
)

AREA_FILTER = '{"PriceArea":["DK1","DK2"]}'


def _parse_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rec in records:
        raw_ts = rec.get("TimeUTC")
        area = rec.get("PriceArea")
        if raw_ts is None or area is None:
            continue

        col_map = EXCHANGE_COLUMNS.get(area)
        if not col_map:
            continue

        ts = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=UTC)

        for col_name, connection in col_map.items():
            flow = rec.get(col_name)
            if flow is None:
                continue
            rows.append(
                {
                    "connection": connection,
                    "ts": ts,
                    "flow_mw": float(flow),
                    "capacity_mw": None,
                    "source": SOURCE,
                }
            )
    return rows


async def _upsert_flows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = text("""
        INSERT INTO crossborder_observations
            (connection, ts, flow_mw, capacity_mw, source)
        VALUES
            (:connection, :ts, :flow_mw, :capacity_mw, :source)
        ON CONFLICT (connection, ts, source) DO NOTHING
    """)
    async with get_session() as session:
        await session.execute(stmt, rows)
        await session.commit()
    return len(rows)


async def ingest_crossborder_flows(days: int = 7) -> None:
    now = datetime.now(UTC)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    logger.info("Ingesting crossborder flows for last %d days", days)

    records = await eds_get(
        DATASET,
        {
            "start": start.strftime("%Y-%m-%dT%H:%M"),
            "end": now.strftime("%Y-%m-%dT%H:%M"),
            "filter": AREA_FILTER,
            "columns": ALL_COLUMNS,
            "sort": "TimeUTC asc",
            "limit": "0",
        },
    )

    if not records:
        logger.warning("No crossborder flow records returned")
        return

    rows = _parse_records(records)
    count = await _upsert_flows(rows)
    logger.info("Crossborder flow ingestion complete: %d rows upserted", count)


async def backfill_crossborder(days: int = 365) -> None:
    now = datetime.now(UTC)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    logger.info("Backfilling crossborder flows for %d days", days)
    total = 0

    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=90), end)

        records = await eds_get(
            DATASET,
            {
                "start": chunk_start.strftime("%Y-%m-%dT%H:%M"),
                "end": chunk_end.strftime("%Y-%m-%dT%H:%M"),
                "filter": AREA_FILTER,
                "columns": ALL_COLUMNS,
                "sort": "TimeUTC asc",
                "limit": "0",
            },
        )

        rows = _parse_records(records)
        count = await _upsert_flows(rows)
        total += count
        logger.info(
            "Backfill crossborder %s→%s: %d rows",
            chunk_start.date(),
            chunk_end.date(),
            count,
        )
        chunk_start = chunk_end

    logger.info("Crossborder backfill complete: %d total rows upserted", total)
