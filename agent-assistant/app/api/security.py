"""Authentication and network boundary for the local control plane."""

from __future__ import annotations

import ipaddress
import secrets

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


bearer = HTTPBearer(auto_error=False)


def require_local_control(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = None,
) -> None:
    client = request.client
    try:
        peer = ipaddress.ip_address(client.host if client is not None else "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="local control plane only",
        ) from exc
    if not peer.is_loopback:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="local control plane only",
        )
    expected = request.app.state.control_token
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(credentials.credentials, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid control token",
            headers={"WWW-Authenticate": "Bearer"},
        )
