"""Firestore-backed durable storage for poll state and anonymous ballots.

Documents are keyed by ``{question_id}__{voter_id}`` so a repeated or corrected
answer overwrites the voter's previous ballot instead of stuffing the box. The
voter identifier is a random value minted by the browser: no account, address,
or user agent is ever written, so a stored ballot cannot be traced to a person.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP, AsyncClient

from vibepoll.store import Phase, PollState, VoteRecord

__all__ = ["FirestoreRepository"]

_STATE_COLLECTION = "polls"
_VOTES_SUBCOLLECTION = "votes"
_DELETE_CHUNK = 400


def _document_id(question_id: str, voter_id: str) -> str:
    return f"{question_id}__{voter_id}"


class FirestoreRepository:
    """Durable :class:`~vibepoll.store.Repository` implementation."""

    def __init__(self, project_id: str, poll_id: str) -> None:
        self._client = AsyncClient(project=project_id)
        self._poll_id = poll_id

    @property
    def _state_document(self) -> Any:
        return self._client.collection(_STATE_COLLECTION).document(self._poll_id)

    @property
    def _votes_collection(self) -> Any:
        return self._state_document.collection(_VOTES_SUBCOLLECTION)

    async def load_state(self) -> PollState | None:
        snapshot = await self._state_document.get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        try:
            phase = Phase(str(data.get("phase", Phase.SLIDESHOW)))
        except ValueError:
            # A document written by an older or newer build carries a phase this
            # one does not know. Startup must not crash on the night, so fall
            # back to the slideshow: voting open, nothing revealed prematurely.
            phase = Phase.SLIDESHOW
        index = data.get("index", 0)
        return PollState(phase=phase, index=index if isinstance(index, int) and index >= 0 else 0)

    async def save_state(self, state: PollState) -> None:
        await self._state_document.set(
            {"phase": str(state.phase), "index": state.index, "updated_at": SERVER_TIMESTAMP}
        )

    async def load_votes(self) -> Iterable[VoteRecord]:
        records: list[VoteRecord] = []
        async for snapshot in self._votes_collection.stream():
            data = snapshot.to_dict() or {}
            question_id, option_id = data.get("question"), data.get("option")
            voter_id = data.get("voter")
            text = data.get("text")
            if isinstance(question_id, str) and isinstance(voter_id, str):
                if isinstance(option_id, str) and text is None:
                    records.append(VoteRecord(question_id=question_id, voter_id=voter_id, option_id=option_id))
                elif isinstance(text, str) and option_id is None:
                    records.append(VoteRecord(question_id=question_id, voter_id=voter_id, text=text))
        return records

    async def save_vote(self, vote: VoteRecord) -> None:
        await self._votes_collection.document(_document_id(vote.question_id, vote.voter_id)).set(
            {
                "question": vote.question_id,
                "voter": vote.voter_id,
                **({"text": vote.text} if vote.text is not None else {"option": vote.option_id}),
                "at": SERVER_TIMESTAMP,
            }
        )

    async def clear_votes(self) -> None:
        batch = self._client.batch()
        pending = 0
        async for snapshot in self._votes_collection.stream():
            batch.delete(snapshot.reference)
            pending += 1
            if pending == _DELETE_CHUNK:
                await batch.commit()
                batch, pending = self._client.batch(), 0
        if pending:
            await batch.commit()

    async def close(self) -> None:
        # AsyncClient inherits the synchronous Client.close(), which releases the
        # HTTP session but not the gRPC transport the async client actually uses.
        # Process exit reclaims the rest; there is no public async teardown.
        self._client.close()
