"""Jinja environment and template helpers shared by the HTML routes."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.models import ACTIVE_STATUSES
from app.providers.base import split_label
from app.services.progress import format_duration

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"


def _static_version() -> str:
    """Short content hash of the files that change with the UI, used as a cache-busting query string."""
    digest = hashlib.sha256()
    for name in ("app.css", "app.js", "vendor/icons.svg"):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:10]


STATIC_VERSION = _static_version()
_UNITS = (("B", 1e9), ("M", 1e6), ("K", 1e3))


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def compact(n: float) -> str:
    """48_200_000 -> '48.2M'; rounds up into the next unit instead of showing '1000K'."""
    for i, (suffix, size) in enumerate(_UNITS):
        if abs(n) >= size:
            value = round(n / size, 1)
            if value >= 1000 and i > 0:
                suffix, value = _UNITS[i - 1][0], round(n / _UNITS[i - 1][1], 1)
            return f"{value:g}{suffix}"
    return f"{n:,.0f}"


def ago(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "—"
    seconds = (now - value).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return f"{days} d ago" if days < 60 else value.strftime("%Y-%m-%d")


def sparkline(points: list[float], width: int = 96, height: int = 24) -> Markup:
    if len(points) < 2:
        return Markup('<span class="muted">—</span>')
    lo, hi = min(points), max(points)
    step = width / (len(points) - 1)

    def y(p: float) -> float:
        return height / 2 if hi == lo else height - 2 - (p - lo) / (hi - lo) * (height - 4)

    coords = " ".join(f"{i * step:.1f},{y(p):.1f}" for i, p in enumerate(points))
    tone = "up" if points[-1] >= points[0] else "down"
    return Markup(
        f'<svg class="spark spark-{tone}" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'aria-hidden="true"><polyline points="{coords}"/></svg>'
    )


def icon(name: str) -> Markup:
    return Markup(
        f'<svg class="icon" aria-hidden="true"><use href="/static/vendor/icons.svg?v={STATIC_VERSION}#i-{name}"/></svg>'
    )


FLASH_COOKIE = "flash"
_CLEAR_FLASH = f"{FLASH_COOKIE}=; Max-Age=0; Path=/; SameSite=lax; HttpOnly"


def safe_next(value: str | None, default: str) -> str:
    """Only same-site paths: '/x' is fine, '//host' and '/\\host' are protocol-relative URLs browsers follow off-site."""
    if value and value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return default


def redirect(url: str, notice: str | None = None) -> RedirectResponse:
    """303 after a POST; `notice` becomes a one-shot toast on the next full page."""
    response = RedirectResponse(url, status_code=303)
    if notice:
        response.set_cookie(FLASH_COOKIE, quote(notice, safe=""), max_age=30, path="/", samesite="lax", httponly=True)
    return response


class FlashCleaner:
    """Deletes the flash cookie when a full HTML page (which shows it as a toast) is sent.

    htmx requests (polls, fragments) are left alone so a poll cannot swallow a toast before the page renders.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if f"{FLASH_COOKIE}=" not in headers.get("cookie", "") or "hx-request" in headers:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                if response_headers.get("content-type", "").startswith("text/html"):
                    response_headers.append("set-cookie", _CLEAR_FLASH)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _page_context(request: Request) -> dict:
    raw = request.cookies.get(FLASH_COOKIE)
    return {
        "now": request.app.state.services.clock.now(),
        "static_version": STATIC_VERSION,
        "flash": unquote(raw)[:200] if raw else None,
    }


_PAGE_ERRORS = {404: "Page not found.", 405: "This page doesn't accept that request."}


async def html_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Browsers get 404/405 as the styled message page; API calls and htmx requests keep FastAPI's JSON errors."""
    wants_page = "text/html" in request.headers.get("accept", "") and "hx-request" not in request.headers
    if exc.status_code not in _PAGE_ERRORS or not wants_page:
        return await http_exception_handler(request, exc)
    generic = exc.detail in (None, "Not Found", "Method Not Allowed")
    message = _PAGE_ERRORS[exc.status_code] if generic else str(exc.detail)
    return templates.TemplateResponse(request, "message.html", {"message": message}, status_code=exc.status_code)


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"), context_processors=[_page_context])
templates.env.filters |= {
    "dt": fmt_dt,
    "duration": format_duration,
    "num": lambda n: f"{n:,}",
    "compact": compact,
    "ago": ago,
    "short_label": lambda label: split_label(label)[0],
}
templates.env.globals |= {"icon": icon, "sparkline": sparkline, "ACTIVE_STATUSES": ACTIVE_STATUSES}
