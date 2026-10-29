from __future__ import annotations

import base64
import json
from datetime import datetime

from fastapi import HTTPException
from pydantic import AwareDatetime, TypeAdapter


def encode_cursor(timestamp: datetime, identifier: object) -> str:
    return base64.urlsafe_b64encode(
        json.dumps([timestamp.isoformat(), str(identifier)]).encode()
    ).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        timestamp, identifier = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Invalid identifier")
        return TypeAdapter(AwareDatetime).validate_python(timestamp), identifier
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid pagination cursor") from exc
