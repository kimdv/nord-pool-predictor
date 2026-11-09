"""Day-ahead demand snapshots; historical availability is never reconstructed."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from nordpool_predictor.config import get_settings
from nordpool_predictor.database import get_session

logger = logging.getLogger(__name__)
API_URL = "https://web-api.tp.entsoe.eu/api"
SOURCE = "entsoe_day_ahead_load"
DOMAINS = {"DK1": "10YDK-1--------W", "DK2": "10YDK-2--------M"}
RESOLUTIONS = {"PT15M": 15, "PT30M": 30, "PT60M": 60}


def _utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("ENTSO-E timestamp is missing its timezone")
    return stamp.astimezone(UTC)


def _request_document(area: str, start: datetime, end: datetime) -> bytes:
    root = ET.Element(
        "StatusRequest_MarketDocument",
        xmlns="urn:iec62325.351:tc57wg16:451-5:statusrequestdocument:4:0",
    )
    for name, value in (
        ("mRID", uuid.uuid4().hex),
        ("type", "A59"),
        ("sender_MarketParticipant.mRID", "10X1001A1001A450"),
        ("sender_MarketParticipant.marketRole.type", "A07"),
        ("receiver_MarketParticipant.mRID", "10X1001A1001A450"),
        ("receiver_MarketParticipant.marketRole.type", "A32"),
        ("createdDateTime", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ):
        element = ET.SubElement(root, name)
        element.text = value
        if name.endswith(".mRID"):
            element.set("codingScheme", "A01")
    for name, value in (
        ("DocumentType", "A65"),
        ("ProcessType", "A01"),
        ("OutBiddingZone_Domain", DOMAINS[area]),
        ("TimeInterval", f"{start:%Y-%m-%dT%H:%MZ}/{end:%Y-%m-%dT%H:%MZ}"),
    ):
        attribute = ET.SubElement(root, "AttributeInstanceComponent")
        ET.SubElement(attribute, "attribute").text = name
        ET.SubElement(attribute, "attributeValue").text = value
    return ET.tostring(root)


def _parse_forecast(xml: bytes, area: str, received_at: datetime) -> list[dict]:
    root = ET.fromstring(xml)
    # Namespace versions vary; all paths below refer to the same document.
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.findtext("type") != "A65" or root.findtext("process.processType") != "A01":
        raise ValueError("Expected an ENTSO-E day-ahead total load forecast")
    source_issued = _utc(root.findtext("createdDateTime", ""))
    if source_issued > received_at:
        raise ValueError("ENTSO-E document was created after receipt")
    rows = {}
    for series in root.findall("TimeSeries"):
        if (
            series.findtext("outBiddingZone_Domain.mRID") != DOMAINS[area]
            or series.findtext("quantity_Measure_Unit.name") != "MAW"
            or series.findtext("businessType") != "A04"
        ):
            raise ValueError("Unexpected ENTSO-E demand zone, unit or business type")
        curve = series.findtext("curveType")
        if curve not in {"A01", "A03"}:
            raise ValueError("Unsupported ENTSO-E curve type")
        for period in series.findall("Period"):
            start = _utc(period.findtext("timeInterval/start", ""))
            end = _utc(period.findtext("timeInterval/end", ""))
            minutes = RESOLUTIONS.get(period.findtext("resolution"))
            if minutes is None or not timedelta(0) < end - start <= timedelta(days=3):
                raise ValueError("Unsupported ENTSO-E forecast interval")
            steps = (end - start).total_seconds() / (minutes * 60)
            if not steps.is_integer() or start.timestamp() % 900:
                raise ValueError("ENTSO-E forecast is not aligned to the quarter-hour grid")
            points = sorted(
                (int(point.findtext("position", "")), float(point.findtext("quantity", "")))
                for point in period.findall("Point")
            )
            positions = [position for position, _ in points]
            if len(set(positions)) != len(positions):
                raise ValueError("Duplicate ENTSO-E forecast position")
            for i, (position, value) in enumerate(points):
                if not 1 <= position <= steps or not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid ENTSO-E demand forecast value or position")
                stop = points[i + 1][0] if curve == "A03" and i + 1 < len(points) else steps + 1
                if curve == "A01":
                    stop = position + 1
                # Expand the interval's average MW, never interpolate across missing blocks.
                for quarter in range(
                    (position - 1) * minutes // 15, int(stop - 1) * minutes // 15
                ):
                    ts = start + timedelta(minutes=quarter * 15)
                    if ts in rows and rows[ts]["load_mw"] != value:
                        raise ValueError("Conflicting ENTSO-E demand forecast intervals")
                    rows[ts] = {
                        "area": area,
                        "ts": ts,
                        "issued_at": received_at,
                        "source_issued_at": source_issued,
                        "load_mw": value,
                        "source": SOURCE,
                    }
    if not rows:
        raise ValueError("No ENTSO-E demand forecast values returned")
    return [rows[ts] for ts in sorted(rows)]


async def ingest_demand_forecasts() -> None:
    settings = get_settings()
    token = settings.entsoe_api_token.get_secret_value()
    if not token:
        logger.info("Demand forecasts disabled: ENTSOE_API_TOKEN is not configured")
        return
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    async with httpx.AsyncClient(timeout=60) as client:
        for area in settings.area_codes:
            document = _request_document(area, start, end)
            for attempt in range(3):
                try:
                    response = await client.post(
                        API_URL,
                        content=document,
                        headers={"Content-Type": "application/xml", "SECURITY_TOKEN": token},
                    )
                    response.raise_for_status()
                    break
                except httpx.HTTPStatusError, httpx.TransportError:
                    if attempt == 2:
                        raise RuntimeError(f"ENTSO-E demand request failed for {area}") from None
                    await asyncio.sleep(2 ** (attempt + 1))
            received_at = datetime.now(UTC)
            rows = [
                row
                for row in _parse_forecast(response.content, area, received_at)
                if start <= row["ts"] < end and row["ts"] >= received_at
            ]
            if not rows:
                raise ValueError(f"No future ENTSO-E demand forecasts returned for {area}")
            async with get_session() as session:
                await session.execute(
                    text("""
                        INSERT INTO demand_forecasts
                            (area, ts, issued_at, source_issued_at, load_mw, source)
                        VALUES (:area, :ts, :issued_at, :source_issued_at, :load_mw, :source)
                        ON CONFLICT DO NOTHING
                    """),
                    rows,
                )
                await session.commit()
            logger.info("Captured %d demand forecast quarters for %s", len(rows), area)
