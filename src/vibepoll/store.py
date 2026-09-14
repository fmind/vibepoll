"""Live poll state, vote tallies, and the persistence boundary.

One Cloud Run instance serves the whole room. Votes are persisted before they
enter the live tally or reach subscribers. Firestore is read back at startup
so a restart resumes from confirmed answers.

The run of show has three phases. During ``SLIDESHOW`` the room answers every
question at its own pace while the stage screen cycles through them with live
results. ``REVIEW`` closes voting and hands the pace to the presenter, one
question at a time. ``FINISHED`` thanks the room and keeps voting closed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypedDict

from vibepoll.config import PollConfig, Question

__all__ = [
    "MemoryRepository",
    "OptionResult",
    "Phase",
    "Poll",
    "PollState",
    "QuestionResults",
    "Repository",
    "VoteRecord",
]


class Phase(StrEnum):
    """Where the room is in the run of show."""

    SLIDESHOW = "slideshow"
    REVIEW = "review"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class PollState:
    """The presenter-controlled position in the run of show."""

    phase: Phase = Phase.SLIDESHOW
    index: int = 0

    @property
    def voting_open(self) -> bool:
        """Whether ballots are accepted.

        Voting is open for the whole slideshow and closed the moment the
        presenter takes the stage, so the numbers being discussed cannot shift
        underneath the commentary.
        """
        return self.phase is Phase.SLIDESHOW


class OptionResult(TypedDict):
    """One option's share of the vote, as rendered on a bar."""

    id: str
    count: int
    percent: float
    winner: bool


class QuestionResults(TypedDict):
    """Every option's standing for one question."""

    total: int
    rows: list[OptionResult]


@dataclass(frozen=True, slots=True)
class VoteRecord:
    """One anonymous ballot: a random client identifier and a chosen option."""

    question_id: str
    voter_id: str
    option_id: str


class Repository(Protocol):
    """Durable storage for poll state and ballots."""

    async def load_state(self) -> PollState | None: ...

    async def save_state(self, state: PollState) -> None: ...

    async def load_votes(self) -> Iterable[VoteRecord]: ...

    async def save_vote(self, vote: VoteRecord) -> None: ...

    async def clear_votes(self) -> None: ...

    async def close(self) -> None: ...


class MemoryRepository:
    """Non-durable repository used by tests and by local runs without GCP."""

    def __init__(self) -> None:
        self._state: PollState | None = None
        self._votes: dict[tuple[str, str], VoteRecord] = {}

    async def load_state(self) -> PollState | None:
        return self._state

    async def save_state(self, state: PollState) -> None:
        self._state = state

    async def load_votes(self) -> Iterable[VoteRecord]:
        return list(self._votes.values())

    async def save_vote(self, vote: VoteRecord) -> None:
        self._votes[(vote.question_id, vote.voter_id)] = vote

    async def clear_votes(self) -> None:
        self._votes.clear()

    async def close(self) -> None:
        return None


class Poll:
    """Authoritative in-memory poll shared by every connected client."""

    def __init__(self, repository: Repository, config: PollConfig) -> None:
        self._repository = repository
        self._config = config
        self._state = PollState()
        # question id -> voter id -> option id. Keeping the raw ballots rather
        # than running counters means a changed answer can never drift the
        # tally: every snapshot recounts from the ballots that actually exist.
        self._ballots: dict[str, dict[str, str]] = {question.id: {} for question in config.questions}
        self._subscribers: set[asyncio.Queue[None]] = set()
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def restore(self) -> None:
        """Rebuild state and tallies from durable storage at startup."""
        stored = await self._repository.load_state()
        if stored is not None:
            self._state = PollState(phase=stored.phase, index=min(stored.index, len(self._config.questions) - 1))
        for vote in await self._repository.load_votes():
            # A ballot for a question the current definition no longer has is
            # dropped rather than resurrected: the deck is the source of truth.
            if vote.question_id in self._ballots:
                self._ballots[vote.question_id][vote.voter_id] = vote.option_id

    async def close(self) -> None:
        await self._repository.close()

    # -- reads -------------------------------------------------------------

    @property
    def state(self) -> PollState:
        return self._state

    def tally(self, question_id: str) -> Counter[str]:
        """Count ballots for one question."""
        return Counter(self._ballots.get(question_id, {}).values())

    def voted_count(self, question_id: str) -> int:
        return len(self._ballots.get(question_id, {}))

    def participants(self) -> int:
        """Distinct anonymous voters seen across the whole deck."""
        return len({voter for ballots in self._ballots.values() for voter in ballots})

    def answers_of(self, voter_id: str) -> dict[str, str]:
        """Every answer this voter has given, keyed by question."""
        return {question_id: ballots[voter_id] for question_id, ballots in self._ballots.items() if voter_id in ballots}

    def results(self, question: Question) -> QuestionResults:
        """Per-option standings in the configured answer order.

        Option labels are deliberately absent: clients fetch the deck once from
        ``/api/poll`` and join on the id, which keeps every streamed frame small
        enough to broadcast on each vote.
        """
        counts = self.tally(question.id)
        total = sum(counts.values())
        top = max(counts.values(), default=0)
        # A winner exists only when a single option leads outright. Without the
        # uniqueness check an empty question crowns every bar on zero, and a
        # genuine tie lights the whole board — which tells the room nothing.
        outright = total > 0 and sum(1 for option in question.options if counts.get(option.id, 0) == top) == 1
        rows: list[OptionResult] = [
            OptionResult(
                id=option.id,
                count=counts.get(option.id, 0),
                percent=round(100 * counts.get(option.id, 0) / total, 1) if total else 0.0,
                winner=outright and counts.get(option.id, 0) == top,
            )
            for option in question.options
        ]
        return QuestionResults(total=total, rows=rows)

    def snapshot(self, *, voter_id: str | None = None) -> dict[str, Any]:
        """Build the payload broadcast over SSE.

        Results are public: the room watches them build on the stage screen
        through the whole break, so there is nothing to withhold from a phone.
        """
        return {
            "phase": str(self._state.phase),
            "index": self._state.index,
            "total": len(self._config.questions),
            "votingOpen": self._state.voting_open,
            "participants": self.participants(),
            "results": {question.id: self.results(question) for question in self._config.questions},
            "answers": self.answers_of(voter_id) if voter_id is not None else {},
        }

    # -- writes ------------------------------------------------------------

    async def cast(self, question_id: str, voter_id: str, option_id: str) -> None:
        """Record one ballot, replacing any earlier answer to the same question.

        Any question accepts a ballot while voting is open: the room answers at
        its own pace on their phones, independently of whichever slide the stage
        screen happens to be showing.

        Raises:
            ValueError: voting is closed, the question is unknown, or the option
                is not part of that question's closed answer set.
        """
        async with self._lock:
            if not self._state.voting_open:
                msg = "voting is closed"
                raise ValueError(msg)
            question = self._config.question(question_id)
            if question is None:
                msg = f"unknown question: {question_id}"
                raise ValueError(msg)
            if option_id not in question.option_ids():
                msg = "unknown option for this question"
                raise ValueError(msg)
            vote = VoteRecord(question_id=question_id, voter_id=voter_id, option_id=option_id)
            # Serialize durable writes with controls and other ballots: failed
            # saves never enter the tally, and a late write cannot undo a change
            # or resurrect a ballot after a wipe. Reads remain non-blocking.
            await self._repository.save_vote(vote)
            self._ballots[question_id][voter_id] = option_id
            self._publish()

    async def advance(self, action: str) -> None:
        """Apply a presenter action only after storage confirms it."""
        async with self._lock:
            state = _next_state(self._state, action, len(self._config.questions) - 1)
            if action == "clear":
                await self._repository.clear_votes()
                for ballots in self._ballots.values():
                    ballots.clear()
            await self._repository.save_state(state)
            self._state = state
            self._publish()

    # -- fan-out -----------------------------------------------------------

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[None]]:
        """Attach a wake-up queue for the lifetime of one SSE connection."""
        # One slot, carrying no payload: it is a "something changed" flag, not a
        # message log. A burst of votes collapses into a single re-render, and a
        # subscriber that has not drained it yet is already going to rebuild
        # from current state, so there is nothing to queue behind it.
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def _publish(self) -> None:
        """Flag every subscriber so it re-renders from its own view of state.

        Each connection rebuilds its snapshot with its own voter identity, so
        one phone's answers are never handed to another's queue.
        """
        for queue in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)


def _next_state(state: PollState, action: str, last: int) -> PollState:
    """Return the state produced by ``action``; unknown actions are rejected."""
    match action:
        case "slideshow" | "clear":
            return PollState(phase=Phase.SLIDESHOW, index=0)
        case "review":
            return PollState(phase=Phase.REVIEW, index=0)
        case "next":
            return PollState(phase=Phase.REVIEW, index=min(state.index + 1, last))
        case "prev":
            return PollState(phase=Phase.REVIEW, index=max(state.index - 1, 0))
        case "finish":
            return PollState(phase=Phase.FINISHED, index=state.index)
        case _:
            msg = f"unknown presenter action: {action}"
            raise ValueError(msg)
