"""Guard against cross-site request forgery: the app has no authentication, so browsers must not be
tricked into sending state-changing requests from another site."""
from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class CrossSiteGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS and self._is_cross_site(scope):
            response = PlainTextResponse("Cross-site request blocked", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _is_cross_site(scope: Scope) -> bool:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if "sec-fetch-site" in headers:
            if headers["sec-fetch-site"] == "cross-site":
                return True
        origin = headers.get("origin")
        if origin is not None:
            origin_host = origin.split("://", 1)[-1].rstrip("/")
            return origin_host != headers.get("host", "")
        return False
