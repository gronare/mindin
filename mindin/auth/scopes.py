from __future__ import annotations

from mcp.server.auth.middleware.auth_context import get_access_token

from mindin.auth import WRITE_SCOPE


class ScopeError(Exception):
    pass


def require_write() -> None:
    token = get_access_token()
    if token is not None and WRITE_SCOPE not in token.scopes:
        raise ScopeError(f"this token may only read the vault; writing needs the {WRITE_SCOPE} scope")
