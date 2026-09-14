"""Free-text validation, durable confirmation, and recovery."""

from __future__ import annotations

import pytest
from litestar.testing import TestClient

from tests.conftest import BASE_URL, make_config
from vibepoll.app import create_app
from vibepoll.config import PollConfig, Question
from vibepoll.settings import Settings
from vibepoll.store import MemoryRepository, Poll, VoteRecord


@pytest.fixture
def text_config() -> PollConfig:
    config = make_config()
    return config.model_copy(
        update={"questions": (*config.questions, Question(id="message", title="Message", type="text"))}
    )


def test_text_api_validates_the_answer_type_and_length(settings: Settings, text_config: PollConfig) -> None:
    app = create_app(settings=settings, config=text_config, repository=MemoryRepository())
    with TestClient(app=app, base_url=BASE_URL) as client:
        base = {"question": "message", "voter": "text-voter"}
        for invalid in (
            {},
            {"text": ""},
            {"text": " \n "},
            {"text": "x" * 501},
            {"text": 42},
            {"option": "o0"},
            {"option": "o0", "text": "Hi"},
        ):
            assert client.post("/api/vote", json={**base, **invalid}).status_code == 400
        assert client.post("/api/vote", json={**base, "question": "q0", "text": "Hi"}).status_code == 400
        assert client.post("/api/vote", json={**base, "text": "x" * 500}).status_code == 200
        assert client.post("/api/vote", json={**base, "text": "  Thanks!  "}).status_code == 200
        assert app.state.poll.answers_of("text-voter") == {"message": "Thanks!"}
        question = client.get("/api/poll").json()["questions"][-1]
        assert question["type"] == "text"
        assert question["maxLength"] == 500


async def test_message_edits_restart_and_wipe(text_config: PollConfig) -> None:
    repository = MemoryRepository()
    poll = Poll(repository, text_config)
    await poll.cast("q0", "voter", "o0")
    await poll.cast("message", "voter", text="First draft")
    await poll.cast("message", "voter", text="Thanks!\nMore demos, please.")
    revived = Poll(repository, text_config)
    await revived.restore()
    assert revived.answers_of("voter") == {"q0": "o0", "message": "Thanks!\nMore demos, please."}
    assert revived.snapshot()["results"]["message"] == {
        "total": 1,
        "rows": [],
        "messages": ["Thanks!\nMore demos, please."],
    }
    assert revived.snapshot()["answers"] == {}
    await revived.advance("review")
    with pytest.raises(ValueError, match="voting is closed"):
        await revived.cast("message", "voter", text="Too late")
    await revived.advance("clear")
    assert revived.snapshot()["results"]["message"]["messages"] == []
    assert list(await repository.load_votes()) == []


async def test_failed_message_save_is_not_published(text_config: PollConfig) -> None:
    class UnavailableRepository(MemoryRepository):
        async def save_vote(self, vote: VoteRecord) -> None:
            raise OSError(f"storage unavailable for {vote.question_id}")

    poll = Poll(UnavailableRepository(), text_config)
    async with poll.subscribe() as queue:
        with pytest.raises(OSError, match="storage unavailable"):
            await poll.cast("message", "voter", text="Thanks!")
        assert poll.answers_of("voter") == {}
        assert poll.snapshot()["results"]["message"]["total"] == 0
        assert queue.empty()
