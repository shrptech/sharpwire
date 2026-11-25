"""Dependencies two or more route modules share."""

import hmac
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.errors import AppHTTPException


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_bearer = HTTPBearer(auto_error=False)


async def require_token(
    settings: SettingsDep, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> None:
    """The read-only API's bearer token (API_TOKEN), until the operator UI's Kanidm login (step 5)."""
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), settings.api_token.encode()):
        raise AppHTTPException(401, "Send the API token as a Bearer token.", error_code="unauthorized")
