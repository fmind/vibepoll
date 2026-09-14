"""Firestore document shapes, verified against a recording stub.

The stub stands in for the client so these assertions pin the things a real
outage would hide until the night: the document id that makes a repeated vote
idempotent, the field names read back at startup, and the chunked delete.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import vibepoll.firestore as firestore_module
from vibepoll.firestore import FirestoreRepository
from vibepoll.store import Phase, PollState, VoteRecord


class FakeDocument:
    def __init__(self, store: dict[str, Any], path: str) -> None:
        self._store = store
        self._path = path

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._store, f"{self._path}/{name}")

    async def set(self, data: dict[str, Any]) -> None:
        self._store[self._path] = data

    async def get(self) -> FakeSnapshot:
        return FakeSnapshot(self._path, self._store.get(self._path))

    @property
    def reference(self) -> FakeDocument:
        return self


class FakeSnapshot:
    def __init__(self, path: str, data: dict[str, Any] | None) -> None:
        self._path = path
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return self._data

    @property
    def reference(self) -> FakeDocument:
        return FakeDocument({}, self._path)


class FakeCollection:
    def __init__(self, store: dict[str, Any], path: str) -> None:
        self._store = store
        self._path = path

    def document(self, document_id: str) -> FakeDocument:
        return FakeDocument(self._store, f"{self._path}/{document_id}")

    async def stream(self) -> Any:
        for path, data in list(self._store.items()):
            if path.startswith(f"{self._path}/"):
                yield FakeSnapshot(path, data)


class FakeBatch:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self.pending: list[str] = []
        self.commits = 0

    def delete(self, reference: FakeDocument) -> None:
        self.pending.append(reference._path)  # noqa: SLF001 - stub mirrors the client's internals

    async def commit(self) -> None:
        self.commits += 1
        for path in self.pending:
            self._store.pop(path, None)
        self.pending.clear()


class FakeClient:
    def __init__(self, project: str) -> None:
        self.project = project
        self.store: dict[str, Any] = {}
        self.batches: list[FakeBatch] = []
        self.closed = False

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self.store, name)

    def batch(self) -> FakeBatch:
        batch = FakeBatch(self.store)
        self.batches.append(batch)
        return batch

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> FirestoreRepository:
    monkeypatch.setattr(firestore_module, "AsyncClient", lambda project: FakeClient(project))
    monkeypatch.setattr(firestore_module, "SERVER_TIMESTAMP", "<timestamp>")
    return FirestoreRepository(project_id="test-project", poll_id="test-poll")


def _client(repository: FirestoreRepository) -> FakeClient:
    """Return the stub standing in for the real Firestore client."""
    return cast("FakeClient", repository._client)  # noqa: SLF001 - reading the injected stub


async def test_state_round_trips(repository: FirestoreRepository) -> None:
    await repository.save_state(PollState(phase=Phase.REVIEW, index=3))
    assert await repository.load_state() == PollState(phase=Phase.REVIEW, index=3)


async def test_missing_state_reads_as_none(repository: FirestoreRepository) -> None:
    assert await repository.load_state() is None


async def test_an_unrecognised_phase_falls_back_to_the_slideshow(repository: FirestoreRepository) -> None:
    """A document from an older build must not crash startup on the night."""
    _client(repository).store["polls/test-poll"] = {"phase": "revealed", "index": 2}
    assert await repository.load_state() == PollState(phase=Phase.SLIDESHOW, index=2)


async def test_a_negative_index_is_clamped(repository: FirestoreRepository) -> None:
    _client(repository).store["polls/test-poll"] = {"phase": "review", "index": -5}
    state = await repository.load_state()
    assert state is not None
    assert state.index == 0


async def test_a_repeated_vote_overwrites_instead_of_stuffing_the_box(repository: FirestoreRepository) -> None:
    await repository.save_vote(VoteRecord(question_id="merger", voter_id="abc", option_id="yes"))
    await repository.save_vote(VoteRecord(question_id="merger", voter_id="abc", option_id="pizza"))

    votes = list(await repository.load_votes())
    assert len(votes) == 1
    assert votes[0].option_id == "pizza"
    assert "polls/test-poll/votes/merger__abc" in _client(repository).store


async def test_a_ballot_stores_no_identifying_fields(repository: FirestoreRepository) -> None:
    await repository.save_vote(VoteRecord(question_id="merger", voter_id="abc", option_id="yes"))
    document = _client(repository).store["polls/test-poll/votes/merger__abc"]
    assert set(document) == {"question", "voter", "option", "at"}


async def test_messages_round_trip_and_can_be_replaced(repository: FirestoreRepository) -> None:
    await repository.save_vote(VoteRecord(question_id="message", voter_id="abc", text="Thanks!"))
    corrected = VoteRecord(question_id="message", voter_id="abc", text="More MCP demos, please!")
    await repository.save_vote(corrected)
    assert list(await repository.load_votes()) == [corrected]
    assert set(_client(repository).store["polls/test-poll/votes/message__abc"]) == {"question", "voter", "text", "at"}


async def test_malformed_ballots_are_skipped_rather_than_crashing_startup(repository: FirestoreRepository) -> None:
    _client(repository).store["polls/test-poll/votes/broken"] = {"question": "merger", "option": 42}
    await repository.save_vote(VoteRecord(question_id="merger", voter_id="abc", option_id="yes"))
    assert len(list(await repository.load_votes())) == 1


async def test_clearing_votes_commits_and_empties_the_collection(repository: FirestoreRepository) -> None:
    for index in range(5):
        await repository.save_vote(VoteRecord(question_id="merger", voter_id=f"v{index}", option_id="yes"))

    await repository.clear_votes()

    assert list(await repository.load_votes()) == []
    assert _client(repository).batches[0].commits == 1


async def test_clearing_an_empty_collection_commits_nothing(repository: FirestoreRepository) -> None:
    await repository.clear_votes()
    assert _client(repository).batches[0].commits == 0


async def test_close_releases_the_client(repository: FirestoreRepository) -> None:
    await repository.close()
    assert _client(repository).closed


async def test_a_large_wipe_commits_in_chunks(repository: FirestoreRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(firestore_module, "_DELETE_CHUNK", 2)
    for index in range(5):
        await repository.save_vote(VoteRecord(question_id="merger", voter_id=f"v{index}", option_id="yes"))

    await repository.clear_votes()

    assert list(await repository.load_votes()) == []
    # Two full chunks plus the remainder, each on its own batch object.
    assert sum(batch.commits for batch in _client(repository).batches) == 3
