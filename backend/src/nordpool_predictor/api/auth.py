from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nordpool_predictor.config import get_settings

_bearer = HTTPBearer(auto_error=False)


def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    token = get_settings().admin_api_token
    if token is None or not token.get_secret_value():
        raise HTTPException(status_code=503, detail="Administrative jobs are disabled")
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), token.get_secret_value().encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="A valid administrator bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
