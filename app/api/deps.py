"""FastAPI dependencies: application state, authentication and authorization."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, Header, Request

from app.api.errors import APIError
from app.config.settings import Settings
from app.persistence.db import Database
from app.persistence.redis_client import RedisCoordinator
from app.persistence.repository import authenticate_api_key


@dataclass
class AppState:
    settings: Settings
    db: Database
    redis: RedisCoordinator


@dataclass(frozen=True)
class Principal:
    id: uuid.UUID
    name: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes


def get_state(request: Request) -> AppState:
    return request.app.state.agentos


def _extract_token(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return None


def require_scopes(*scopes: str) -> Callable[..., Awaitable[Principal]]:
    async def dependency(
        state: AppState = Depends(get_state),
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> Principal:
        token = _extract_token(authorization, x_api_key)
        if not token:
            raise APIError(401, "unauthenticated", "missing API key (Authorization: Bearer <key>)")
        async with state.db.session() as session:
            key = await authenticate_api_key(session, token)
            if key is None:
                raise APIError(401, "unauthenticated", "invalid or revoked API key")
            principal = Principal(key.id, key.name, frozenset(key.scopes))
        missing = [s for s in scopes if not principal.has(s)]
        if missing:
            raise APIError(403, "forbidden", "API key lacks required scopes", {"missing": missing})
        return principal

    return dependency
