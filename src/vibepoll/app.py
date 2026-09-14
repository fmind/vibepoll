"""Composition root: immutable startup state, routes, and the SSE fan-out.

One Cloud Run instance serves the whole room, so the :class:`~vibepoll.store.Poll`
built during lifespan startup is the single authority every connection reads
from. Requests never observe partial state: the poll is restored from durable
storage before the first route is served.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import segno
from litestar import Litestar, MediaType, Request, Response, get, post
from litestar.datastructures import Cookie, ResponseHeader
from litestar.exceptions import NotFoundException, ValidationException
from litestar.params import Body, QueryParameter
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.response import Redirect, ServerSentEvent, ServerSentEventMessage, Template
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from pydantic import BaseModel, Field

from vibepoll.config import MAX_TEXT_LENGTH, PollConfig, load_config
from vibepoll.settings import Settings, load_settings
from vibepoll.store import MemoryRepository, Poll, Repository

__all__ = ["create_app"]

_PACKAGE_ROOT = Path(__file__).parent
_TEMPLATES = _PACKAGE_ROOT / "templates"
_STATIC = _PACKAGE_ROOT / "static"
_PRESENTER_COOKIE = "vp_presenter"
# Long enough that a quiet room does not churn connections, short enough to stay
# under the idle timeouts of intermediate proxies.
_HEARTBEAT_SECONDS = 20.0

logger = logging.getLogger("vibepoll")


class VotePayload(BaseModel):
    """One anonymous ballot submitted by a phone."""

    question: Annotated[str, Field(min_length=1, max_length=64)]
    option: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    text: Annotated[str | None, Field(min_length=1, max_length=MAX_TEXT_LENGTH)] = None
    voter: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class ControlPayload(BaseModel):
    """One presenter command."""

    action: Annotated[str, Field(min_length=1, max_length=16)]


def _matches_key(candidate: str, key: str) -> bool:
    """Compare a presented secret against the presenter key in constant time.

    Bytes, not text: ``secrets.compare_digest`` raises ``TypeError`` on
    non-ASCII strings, which would turn a junk query string into a 500 and make
    the presenter route distinguishable from a genuine 404. ``errors="replace"``
    keeps even a lone surrogate from a malformed URL out of the exception path.
    """
    return bool(candidate) and secrets.compare_digest(candidate.encode(errors="replace"), key.encode())


def _is_presenter(request: Request[Any, Any, Any]) -> bool:
    """Return whether the request carries the presenter cookie."""
    settings: Settings = request.app.state.settings
    return _matches_key(request.cookies.get(_PRESENTER_COOKIE, ""), settings.presenter_key)


def _page_context(request: Request[Any, Any, Any]) -> dict[str, Any]:
    return {"settings": request.app.state.settings, "config": request.app.state.config}


@get("/", name="vote", include_in_schema=False)
async def vote_page(request: Request[Any, Any, Any]) -> Template:
    """Serve the phone voting page."""
    return Template("vote.html", context=_page_context(request))


@get("/present", name="present", include_in_schema=False)
async def present_page(
    request: Request[Any, Any, Any],
    key: Annotated[str | None, QueryParameter(name="key", required=False)] = None,
) -> Response[Any]:
    """Serve the projector page, exchanging a one-time key for a cookie.

    The key is accepted once in the query string and then replaced by an
    HttpOnly cookie and a clean URL, so the shared secret is not left in the
    address bar of a screen that is about to be projected to a room.
    """
    settings: Settings = request.app.state.settings
    if key is not None and _matches_key(key, settings.presenter_key):
        response: Response[Any] = Redirect(path="/present")
        response.set_cookie(
            Cookie(
                key=_PRESENTER_COOKIE,
                value=settings.presenter_key,
                httponly=True,
                secure=settings.is_https,
                samesite="strict",
                max_age=60 * 60 * 12,
                path="/",
            )
        )
        return response
    if not _is_presenter(request):
        # Fail closed and indistinguishable from a typo: an unauthenticated
        # probe learns nothing about whether a presenter console exists.
        raise NotFoundException
    return Template("present.html", context=_page_context(request))


@get("/qr.svg", name="qr", media_type="image/svg+xml", include_in_schema=False, sync_to_thread=False)
def qr_code(request: Request[Any, Any, Any]) -> Response[bytes]:
    """Render the join URL as an inline-ready SVG QR code."""
    settings: Settings = request.app.state.settings
    # A standalone document, not segno's `svg_inline` fragment: an <img src>
    # refuses an SVG without the xmlns declaration, and the QR silently renders
    # as alt text on the projector. High error correction survives a hand or a
    # head in front of the screen; omitting width/height leaves only a viewBox,
    # so the code scales to whatever .qr-frame gives it.
    buffer = io.BytesIO()
    segno.make(settings.public_url, error="h").save(
        buffer, kind="svg", scale=1, dark="#000", omitsize=True, xmldecl=False, svgclass=None, lineclass=None
    )
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@post("/api/vote", name="cast", status_code=200, include_in_schema=False)
async def cast_vote(request: Request[Any, Any, Any], data: Annotated[VotePayload, Body()]) -> dict[str, str]:
    """Record one ballot."""
    poll: Poll = request.app.state.poll
    try:
        await poll.cast(data.question, data.voter, data.option, text=data.text)
    except ValueError as error:
        raise ValidationException(detail=str(error)) from error
    return {"status": "ok"}


@post("/api/control", name="control", status_code=200, include_in_schema=False)
async def control(request: Request[Any, Any, Any], data: Annotated[ControlPayload, Body()]) -> dict[str, str]:
    """Apply one presenter command to the run of show."""
    if not _is_presenter(request):
        raise NotFoundException
    poll: Poll = request.app.state.poll
    try:
        await poll.advance(data.action)
    except ValueError as error:
        raise ValidationException(detail=str(error)) from error
    return {"status": "ok"}


@get("/api/poll", name="poll", include_in_schema=False, sync_to_thread=False)
def poll_definition(request: Request[Any, Any, Any]) -> dict[str, Any]:
    """Serve the deck and its pacing once, at page load.

    Clients join choice ids and counts against this payload. Question prose is
    fetched once; audience messages are included in streamed results.
    """
    config: PollConfig = request.app.state.config
    return {
        "panelSeconds": config.panel_seconds,
        "questions": [
            {
                "id": question.id,
                "title": question.title,
                "subtitle": question.subtitle,
                "type": question.type,
                "maxLength": MAX_TEXT_LENGTH if question.type == "text" else None,
                "options": [{"id": option.id, "label": option.label} for option in question.options],
            }
            for question in config.questions
        ],
    }


@get("/events", name="events", include_in_schema=False)
async def events(
    request: Request[Any, Any, Any],
    voter: Annotated[
        str | None,
        QueryParameter(name="voter", required=False, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    ] = None,
) -> ServerSentEvent:
    """Stream state changes to one phone or projector."""
    poll: Poll = request.app.state.poll
    # `voter` is already length- and charset-bounded by the parameter contract
    # above, so the snapshot can use it directly.
    voter_id = voter or None

    async def stream() -> AsyncGenerator[ServerSentEventMessage]:
        async with poll.subscribe() as queue:
            yield _state_message(poll, voter_id=voter_id)
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ServerSentEventMessage(comment="keep-alive")
                    continue
                yield _state_message(poll, voter_id=voter_id)

    return ServerSentEvent(stream())


def _state_message(poll: Poll, *, voter_id: str | None) -> ServerSentEventMessage:
    payload = poll.snapshot(voter_id=voter_id)
    return ServerSentEventMessage(event="state", data=json.dumps(payload, separators=(",", ":")))


# Not /healthz: Google Frontend intercepts that exact path in front of Cloud
# Run and answers it with its own HTML 404, so the request never reaches the
# container. Verified against the deployed service — every other spelling
# (/health, /livez, /readyz) passes through.
@get("/health", name="health", media_type=MediaType.TEXT, include_in_schema=False, sync_to_thread=False)
def health() -> str:
    """Report liveness for the platform probe."""
    return "ok"


# No inline script or style: every behaviour the pages need is served from
# /static, so a reflected value can never execute. `img-src data:` covers the
# inline favicon only.
_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _security_headers(settings: Settings) -> list[ResponseHeader]:
    """Return the response policy applied to every route."""
    headers = {
        "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if settings.is_https:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return [ResponseHeader(name=name, value=value) for name, value in headers.items()]


def _build_repository(settings: Settings, config: PollConfig) -> Repository:
    """Choose durable storage, falling back to memory when GCP is unconfigured."""
    if settings.project_id is None:
        logger.warning("GOOGLE_CLOUD_PROJECT is unset: votes are kept in memory and lost on restart")
        return MemoryRepository()
    from vibepoll.firestore import FirestoreRepository

    return FirestoreRepository(project_id=settings.project_id, poll_id=config.id)


def create_app(
    settings: Settings | None = None,
    config: PollConfig | None = None,
    repository: Repository | None = None,
) -> Litestar:
    """Build the application with all startup state resolved once."""
    resolved = settings or load_settings()
    deck = config if config is not None else load_config(resolved.config_path)

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:
        store = repository if repository is not None else _build_repository(resolved, deck)
        poll = Poll(store, deck)
        await poll.restore()
        app.state.settings = resolved
        app.state.config = deck
        app.state.poll = poll
        logger.info("serving %r: %d questions from %s", deck.title, len(deck.questions), resolved.config_path)
        if resolved.generated_key:
            logger.warning("PRESENTER_KEY was unset; presenter console: /present?key=%s", resolved.presenter_key)
        try:
            yield
        finally:
            await poll.close()

    return Litestar(
        route_handlers=[
            vote_page,
            present_page,
            qr_code,
            poll_definition,
            cast_vote,
            control,
            events,
            health,
            create_static_files_router(path="/static", directories=[_STATIC], name="static"),
        ],
        template_config=TemplateConfig(directory=_TEMPLATES, engine=JinjaTemplateEngine),
        lifespan=[lifespan],
        response_headers=_security_headers(resolved),
        # No public API surface to document, and /schema would otherwise
        # serve third-party doc bundles that the CSP is written to forbid.
        openapi_config=None,
        debug=False,
    )


# Granian targets this factory with --factory, so importing the module for
# tests never reads the environment or opens a Firestore client as a side effect.
