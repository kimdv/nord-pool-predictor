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
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.energidataservice.dk/dataset"

MAX_RETRIES = 5
BACKOFF_BASE = 2.0
RATE_LIMIT_WAIT = 65.0

_last_request_by_dataset: dict[str, float] = {}
_dataset_locks: dict[str, asyncio.Lock] = {}
_meta_lock: asyncio.Lock | None = None


async def _get_dataset_lock(dataset: str) -> asyncio.Lock:
    """Return a per-dataset lock, creating one lazily if needed."""
    global _meta_lock
    if _meta_lock is None:
        _meta_lock = asyncio.Lock()
    async with _meta_lock:
        if dataset not in _dataset_locks:
            _dataset_locks[dataset] = asyncio.Lock()
        return _dataset_locks[dataset]


async def _enforce_rate_limit(dataset: str) -> None:
    """Wait if we've hit the same dataset within the last 60 seconds.

    Uses a per-dataset lock so different datasets can proceed in parallel.
    """
    lock = await _get_dataset_lock(dataset)
    async with lock:
        last = _last_request_by_dataset.get(dataset, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < 60.0:
            wait = 60.0 - elapsed + 1.0
            logger.info(
                "Rate-limit: waiting %.0fs before next %s request", wait, dataset
            )
            await asyncio.sleep(wait)
        _last_request_by_dataset[dataset] = time.monotonic()


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

    if respect_rate_limit:
        await _enforce_rate_limit(dataset)

    url = f"{API_BASE}/{dataset}"
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60.0)

    try:
        return await _request_with_retry(client, url, params, dataset)
    finally:
        if own_client:
            await client.aclose()


async def _request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    dataset: str,
) -> list[dict[str, Any]]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params, timeout=60.0)
            resp.raise_for_status()
            payload = resp.json()
            return payload.get("records", [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning(
                    "Rate limited (429) on %s, waiting %.0fs",
                    dataset, RATE_LIMIT_WAIT,
                )
                await asyncio.sleep(RATE_LIMIT_WAIT)
                _last_request_by_dataset[dataset] = time.monotonic()
                continue
            if attempt == MAX_RETRIES:
                logger.error(
                    "Request to %s failed after %d attempts: %s",
                    dataset, MAX_RETRIES, exc,
                )
                raise
            delay = BACKOFF_BASE ** attempt
            logger.warning(
                "%s attempt %d/%d failed (%s), retrying in %.1fs",
                dataset, attempt, MAX_RETRIES, exc, delay,
            )
            await asyncio.sleep(delay)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES:
                logger.error(
                    "Request to %s failed after %d attempts: %s",
                    dataset, MAX_RETRIES, exc,
                )
                raise
            delay = BACKOFF_BASE ** attempt
            logger.warning(
                "%s attempt %d/%d failed (%s), retrying in %.1fs",
                dataset, attempt, MAX_RETRIES, exc, delay,
            )
            await asyncio.sleep(delay)
    return []
