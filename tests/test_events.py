"""SSE contract, driven against the ASGI application directly.

Litestar's ``TestClient`` buffers a whole response body before returning it
(``litestar/testing/transport.py``), so it deadlocks on a stream that is meant
to stay open for the length of a talk. These tests speak ASGI instead, which
exercises the real handler, the real presenter check, and the real disclosure
gating without waiting for a stream that never ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, cast

import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient
from litestar.types import Receive, Scope, Send

from tests.conftest import BASE_URL, PRESENTER_KEY
from vibepoll.config import PollConfig

VOTER = "voter0000000001"
_READ_TIMEOUT = 5.0


async def first_state(app: Litestar, *, query: str = "", presenter: bool = False) -> dict[str, Any]:
    """Open ``/events``, return the first state frame, then hang up."""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"poll.example")]
    if presenter:
        headers.append((b"cookie", f"vp_presenter={PRESENTER_KEY}".encode()))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/events",
        "raw_path": b"/events",
        "query_string": query.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("test", 1234),
        "server": ("poll.example", 443),
    }

    chunks: asyncio.Queue[bytes] = asyncio.Queue()
    started: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def receive() -> dict[str, Any]:
        # The client stays connected until the task below is cancelled.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start" and not started.done():
            started.set_result(message["status"])
        elif message["type"] == "http.response.body":
            await chunks.put(message.get("body", b""))

    task = asyncio.create_task(app(cast("Scope", scope), cast("Receive", receive), cast("Send", send)))
    try:
        status = await asyncio.wait_for(started, timeout=_READ_TIMEOUT)
        assert status == 200, f"unexpected status {status}"
        buffer = b""
        async with asyncio.timeout(_READ_TIMEOUT):
            while b"data: " not in buffer:
                buffer += await chunks.get()
        payload = buffer.split(b"data: ", 1)[1].split(b"\r\n", 1)[0]
        return json.loads(payload)
    finally:
        # A rejected request completes on its own; only a live stream needs
        # cancelling, and awaiting it afterwards keeps the loop free of a
        # pending task that would leak into the next test.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
async def live(app: Litestar) -> Any:
    async with AsyncTestClient(app=app, base_url=BASE_URL) as client:
        await client.get("/present", params={"key": PRESENTER_KEY})
        yield client


async def test_stream_opens_with_the_current_state(app: Litestar, live: Any, config: PollConfig) -> None:
    state = await first_state(app)
    assert state["phase"] == "slideshow"
    assert state["votingOpen"] is True
    assert state["total"] == len(config.questions)
    assert set(state["results"]) == {question.id for question in config.questions}
    assert live is not None


async def test_stream_counts_a_ballot_cast_over_http(app: Litestar, live: Any, config: PollConfig) -> None:
    question = config.questions[0]
    response = await live.post(
        "/api/vote", json={"question": question.id, "option": question.options[1].id, "voter": VOTER}
    )
    assert response.status_code == 200

    state = await first_state(app)
    assert state["results"][question.id]["total"] == 1


async def test_results_are_public_during_the_break(app: Litestar, live: Any, config: PollConfig) -> None:
    """The room watches the bars build on the stage screen, so nothing is held back."""
    question = config.questions[0]
    await live.post("/api/vote", json={"question": question.id, "option": question.options[0].id, "voter": VOTER})

    rows = (await first_state(app))["results"][question.id]["rows"]
    assert next(row for row in rows if row["id"] == question.options[0].id)["count"] == 1


async def test_taking_the_stage_closes_voting_for_the_room(app: Litestar, live: Any) -> None:
    await live.post("/api/control", json={"action": "review"})
    state = await first_state(app)
    assert state["phase"] == "review"
    assert state["votingOpen"] is False


async def test_stream_reports_only_the_requesting_voters_answers(app: Litestar, live: Any, config: PollConfig) -> None:
    question = config.questions[0]
    await live.post("/api/vote", json={"question": question.id, "option": question.options[2].id, "voter": VOTER})

    mine = await first_state(app, query=f"voter={VOTER}")
    assert mine["answers"] == {question.id: question.options[2].id}

    theirs = await first_state(app, query="voter=someoneelse00001")
    assert theirs["answers"] == {}


async def test_stream_rejects_a_malformed_voter_identifier(app: Litestar, live: Any) -> None:
    assert live is not None
    with pytest.raises(AssertionError, match="unexpected status 400"):
        await first_state(app, query="voter=../../etc/passwd")
