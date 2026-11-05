"""Shared client for the Energi Data Service API.

Handles:
- ``timezone=UTC`` on every request (the API interprets start/end as
  Danish local time by default).
- ``limit=0`` for unbounded queries (returns all matching records).
- Per-dataset rate limiting (max 1 request per dataset per minute).
- Retry with exponential back-off, with special 429 handling.

Reference: https://www.energidataservice.dk/guides/api-guides
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from sqlalchemy import text

from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)

API_BASE = "https://api.energidataservice.dk/dataset"

MAX_RETRIES = 5
BACKOFF_BASE = 2.0
RATE_LIMIT_WAIT = 65.0


async def _enforce_rate_limit(dataset: str) -> None:
    """Serialize dataset requests across processes using the database clock."""
    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO eds_request_limits (dataset, requested_at)
                VALUES (:dataset, '-infinity') ON CONFLICT DO NOTHING
            """),
            {"dataset": dataset},
        )
        wait = (
            await session.execute(
                text("""
                    SELECT GREATEST(0, EXTRACT(EPOCH FROM (
                        requested_at + INTERVAL '61 seconds' - clock_timestamp()
                    )))::double precision
                    FROM eds_request_limits WHERE dataset = :dataset FOR UPDATE
                """),
                {"dataset": dataset},
            )
        ).scalar_one()
        if wait > 0:
            logger.info("Rate-limit: waiting %.0fs before next %s request", wait, dataset)
            await asyncio.sleep(wait)
        await session.execute(
            text("""
                UPDATE eds_request_limits SET requested_at = clock_timestamp()
                WHERE dataset = :dataset
            """),
            {"dataset": dataset},
        )
        await session.commit()


async def eds_get(
    dataset: str,
    params: dict[str, str],
    *,
    client: httpx.AsyncClient | None = None,
    respect_rate_limit: bool = True,
) -> list[dict[str, Any]]:
    """Execute a GET against the Energi Data Service API.

    Always injects ``timezone=UTC`` so that ``start``/``end`` are
    interpreted as UTC timestamps (the API defaults to Danish local
    time).  Returns the ``records`` list from the JSON response.
    """
    params.setdefault("timezone", "UTC")

    url = f"{API_BASE}/{dataset}"
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60.0)

    try:
        return await _request_with_retry(
            client, url, params, dataset, respect_rate_limit=respect_rate_limit
        )
    finally:
        if own_client:
            await client.aclose()


async def _request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    dataset: str,
    *,
    respect_rate_limit: bool = True,
) -> list[dict[str, Any]]:
    for attempt in range(1, MAX_RETRIES + 1):
        if respect_rate_limit:
            await _enforce_rate_limit(dataset)
        try:
            resp = await client.get(url, params=params, timeout=60.0)
            resp.raise_for_status()
            payload = resp.json()
            return payload.get("records", [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                if attempt == MAX_RETRIES:
                    logger.error("Rate-limit retries exhausted for %s", dataset)
                    raise
                logger.warning(
                    "Rate limited (429) on %s, waiting %.0fs",
                    dataset,
                    RATE_LIMIT_WAIT,
                )
                await asyncio.sleep(RATE_LIMIT_WAIT)
                continue
            if attempt == MAX_RETRIES:
                logger.error(
                    "Request to %s failed after %d attempts: %s",
                    dataset,
                    MAX_RETRIES,
                    exc,
                )
                raise
            delay = BACKOFF_BASE**attempt
            logger.warning(
                "%s attempt %d/%d failed (%s), retrying in %.1fs",
                dataset,
                attempt,
                MAX_RETRIES,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES:
                logger.error(
                    "Request to %s failed after %d attempts: %s",
                    dataset,
                    MAX_RETRIES,
                    exc,
                )
                raise
            delay = BACKOFF_BASE**attempt
            logger.warning(
                "%s attempt %d/%d failed (%s), retrying in %.1fs",
                dataset,
                attempt,
                MAX_RETRIES,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"Request retries exhausted for {dataset}")
