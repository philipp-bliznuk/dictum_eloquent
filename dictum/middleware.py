import logging
import typing as t

import fastapi.responses

from dictum.hosts import HostAllowList
from dictum.hosts import host_from_header

if t.TYPE_CHECKING:
    import collections.abc as cabc

    from starlette.types import ASGIApp
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

log = logging.getLogger("dictum.hosts")


def _host_header(*, scope: Scope) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == b"host":
            return value.decode("latin-1")
    return ""


class AllowedHostsMiddleware:
    """Rejects HTTP requests whose `Host` is outside `ALLOWED_HOSTS`.

    Same `*` / exact / `*.suffix` semantics as Starlette's `TrustedHostMiddleware`, plus loopback
    names always pass, the 400 is JSON naming the host, and every rejection is logged so a wrong
    allow-list is visible in the server log instead of silently failing.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: cabc.Iterable[str]) -> None:
        self.app = app
        self.allow_list = HostAllowList(patterns=allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = host_from_header(value=_host_header(scope=scope))
        if self.allow_list.allows(host=host):
            await self.app(scope, receive, send)
            return
        log.warning("rejected Host %r; ALLOWED_HOSTS=%s", host, ",".join(self.allow_list.patterns))
        response = fastapi.responses.JSONResponse(
            {"detail": f"Host {host!r} is not in ALLOWED_HOSTS"},
            status_code=fastapi.status.HTTP_400_BAD_REQUEST,
        )
        await response(scope, receive, send)
