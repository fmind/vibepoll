"""Tally and run-of-show behaviour that the stage screen depends on."""

from __future__ import annotations

import pytest

from tests.conftest import make_config
from vibepoll.config import PollConfig
from vibepoll.store import MemoryRepository, Phase, Poll, PollState, VoteRecord


@pytest.fixture
def poll(config: PollConfig) -> Poll:
    return Poll(MemoryRepository(), config)


def first(config: PollConfig) -> tuple[str, list[str]]:
    question = config.questions[0]
    return question.id, [option.id for option in question.options]


async def test_the_deck_opens_on_the_slideshow_with_voting_open(poll: Poll) -> None:
    assert poll.state.phase is Phase.SLIDESHOW
    assert poll.state.voting_open


async def test_any_question_accepts_a_ballot_while_voting_is_open(poll: Poll, config: PollConfig) -> None:
    """The room answers at its own pace, not in step with the stage screen."""
    for question in config.questions:
        await poll.cast(question.id, "voter-1", question.options[0].id)
    assert len(poll.answers_of("voter-1")) == len(config.questions)


async def test_questions_can_be_answered_out_of_order(poll: Poll, config: PollConfig) -> None:
    last, head = config.questions[-1], config.questions[0]
    await poll.cast(last.id, "voter-1", last.options[0].id)
    await poll.cast(head.id, "voter-1", head.options[0].id)
    assert set(poll.answers_of("voter-1")) == {head.id, last.id}


async def test_voting_closes_when_the_presenter_takes_the_stage(poll: Poll, config: PollConfig) -> None:
    await poll.advance("review")
    assert not poll.state.voting_open
    question_id, options = first(config)
    with pytest.raises(ValueError, match="voting is closed"):
        await poll.cast(question_id, "voter-1", options[0])


async def test_returning_to_the_slideshow_reopens_voting(poll: Poll, config: PollConfig) -> None:
    await poll.advance("review")
    await poll.advance("slideshow")
    question_id, options = first(config)
    await poll.cast(question_id, "voter-1", options[0])
    assert poll.voted_count(question_id) == 1


async def test_voting_is_closed_on_the_finale(poll: Poll, config: PollConfig) -> None:
    await poll.advance("finish")
    question_id, options = first(config)
    with pytest.raises(ValueError, match="voting is closed"):
        await poll.cast(question_id, "voter-1", options[0])


async def test_an_unknown_question_is_rejected(poll: Poll) -> None:
    with pytest.raises(ValueError, match="unknown question"):
        await poll.cast("not-a-question", "voter-1", "o0")


async def test_an_option_outside_the_question_is_rejected(poll: Poll, config: PollConfig) -> None:
    with pytest.raises(ValueError, match="unknown option"):
        await poll.cast(config.questions[0].id, "voter-1", "write-in-answer")


async def test_changing_an_answer_moves_the_vote_instead_of_adding_one(poll: Poll, config: PollConfig) -> None:
    question_id, options = first(config)
    await poll.cast(question_id, "voter-1", options[0])
    await poll.cast(question_id, "voter-1", options[3])

    tally = poll.tally(question_id)
    assert tally[options[0]] == 0
    assert tally[options[3]] == 1
    assert poll.voted_count(question_id) == 1


async def test_results_mark_a_single_winner(poll: Poll, config: PollConfig) -> None:
    question = config.questions[0]
    for index, voter in enumerate(("a", "b", "c", "d")):
        await poll.cast(question.id, voter, question.options[0 if index < 3 else 1].id)

    results = poll.results(question)
    assert results["total"] == 4
    assert results["rows"][0]["id"] == question.options[0].id
    assert results["rows"][0]["percent"] == 75.0
    assert [row["winner"] for row in results["rows"]] == [True, False, False, False]


async def test_no_option_wins_before_anyone_votes(poll: Poll, config: PollConfig) -> None:
    results = poll.results(config.questions[0])
    assert not any(row["winner"] for row in results["rows"])
    assert all(row["percent"] == 0.0 for row in results["rows"])


async def test_a_tie_crowns_nobody(poll: Poll, config: PollConfig) -> None:
    """A shared lead must not light every bar on the stage screen."""
    question = config.questions[0]
    await poll.cast(question.id, "voter-1", question.options[0].id)
    await poll.cast(question.id, "voter-2", question.options[1].id)
    assert not any(row["winner"] for row in poll.results(question)["rows"])


async def test_breaking_a_tie_restores_a_single_winner(poll: Poll, config: PollConfig) -> None:
    question = config.questions[0]
    await poll.cast(question.id, "voter-1", question.options[0].id)
    await poll.cast(question.id, "voter-2", question.options[1].id)
    await poll.cast(question.id, "voter-3", question.options[1].id)

    rows = poll.results(question)["rows"]
    assert [row["winner"] for row in rows] == [False, True, False, False]
    assert rows[1]["id"] == question.options[1].id


async def test_results_carry_no_labels(poll: Poll, config: PollConfig) -> None:
    """Prose is fetched once from /api/poll, never repeated per frame."""
    rows = poll.results(config.questions[0])["rows"]
    assert set(rows[0]) == {"id", "count", "percent", "winner"}


async def test_snapshot_covers_every_question(poll: Poll, config: PollConfig) -> None:
    payload = poll.snapshot()
    assert set(payload["results"]) == {question.id for question in config.questions}
    assert payload["total"] == len(config.questions)


async def test_a_voter_sees_only_their_own_answers(poll: Poll, config: PollConfig) -> None:
    question_id, options = first(config)
    await poll.cast(question_id, "voter-1", options[0])
    assert poll.snapshot(voter_id="voter-1")["answers"] == {question_id: options[0]}
    assert poll.snapshot(voter_id="voter-2")["answers"] == {}
    assert poll.snapshot()["answers"] == {}


async def test_review_navigation_stays_inside_the_deck(poll: Poll, config: PollConfig) -> None:
    await poll.advance("review")
    await poll.advance("prev")
    assert poll.state.index == 0
    for _ in range(len(config.questions) + 3):
        await poll.advance("next")
    assert poll.state.index == len(config.questions) - 1
    assert poll.state.phase is Phase.REVIEW


async def test_unknown_presenter_action_is_rejected(poll: Poll) -> None:
    with pytest.raises(ValueError, match="unknown presenter action"):
        await poll.advance("launch-missiles")


async def test_participants_counts_distinct_voters_across_questions(poll: Poll, config: PollConfig) -> None:
    head, second = config.questions[0], config.questions[1]
    await poll.cast(head.id, "voter-1", head.options[0].id)
    await poll.cast(head.id, "voter-2", head.options[1].id)
    await poll.cast(second.id, "voter-1", second.options[0].id)
    assert poll.participants() == 2


async def test_state_and_votes_survive_a_restart(config: PollConfig) -> None:
    question = config.questions[0]
    repository = MemoryRepository()
    first_run = Poll(repository, config)
    await first_run.restore()
    await first_run.cast(question.id, "voter-1", question.options[2].id)
    await first_run.advance("review")

    revived = Poll(repository, config)
    await revived.restore()
    assert revived.state == PollState(phase=Phase.REVIEW, index=0)
    assert revived.tally(question.id)[question.options[2].id] == 1


async def test_wiping_votes_clears_storage_and_reopens_voting(config: PollConfig) -> None:
    question = config.questions[0]
    repository = MemoryRepository()
    poll = Poll(repository, config)
    await poll.restore()
    await poll.cast(question.id, "voter-1", question.options[0].id)
    await poll.advance("review")

    await poll.advance("clear")

    assert poll.participants() == 0
    assert list(await repository.load_votes()) == []
    assert poll.state.voting_open


async def test_a_shorter_deck_drops_stale_ballots_and_clamps_the_index() -> None:
    """Editing poll.yaml between sessions must not resurrect removed questions."""
    long_deck, short_deck = make_config(questions=4), make_config(questions=2)
    repository = MemoryRepository()
    before = Poll(repository, long_deck)
    await before.restore()
    for question in long_deck.questions:
        await before.cast(question.id, "voter-1", question.options[0].id)
    await before.advance("review")
    await before.advance("next")
    await before.advance("next")
    assert before.state.index == 2

    after = Poll(repository, short_deck)
    await after.restore()
    assert after.state.index == len(short_deck.questions) - 1
    assert set(after.answers_of("voter-1")) == {question.id for question in short_deck.questions}


async def test_a_slow_subscriber_is_never_dropped_from_the_fan_out(poll: Poll, config: PollConfig) -> None:
    """A one-slot queue collapses a burst; it must not evict the connection.

    Evicting left the stream open but never woken again, so that phone froze on
    stale state until its owner reloaded.
    """
    question = config.questions[0]
    async with poll.subscribe() as queue:
        for index in range(20):
            await poll.cast(question.id, f"voter-{index}", question.options[0].id)
        assert queue.qsize() == 1
        queue.get_nowait()
        await poll.cast(question.id, "voter-late", question.options[1].id)
        assert queue.qsize() == 1


async def test_failed_vote_is_not_counted_or_broadcast(config: PollConfig) -> None:
    class UnavailableRepository(MemoryRepository):
        async def save_vote(self, vote: VoteRecord) -> None:
            raise OSError(f"storage unavailable for {vote.question_id}")

    poll = Poll(UnavailableRepository(), config)
    question_id, options = first(config)
    async with poll.subscribe() as queue:
        with pytest.raises(OSError, match="storage unavailable"):
            await poll.cast(question_id, "voter-1", options[0])
        assert poll.voted_count(question_id) == 0
        assert poll.answers_of("voter-1") == {}
        assert queue.empty()


async def test_failed_control_preserves_the_visible_phase(config: PollConfig) -> None:
    class UnavailableRepository(MemoryRepository):
        async def save_state(self, state: PollState) -> None:
            raise OSError(f"storage unavailable for {state.phase}")

    poll = Poll(UnavailableRepository(), config)
    with pytest.raises(OSError, match="storage unavailable"):
        await poll.advance("review")
    assert poll.state.phase is Phase.SLIDESHOW


async def test_results_preserve_the_answer_scale_order(poll: Poll, config: PollConfig) -> None:
    question = config.questions[0]
    await poll.cast(question.id, "voter-1", question.options[-1].id)
    rows = poll.results(question)["rows"]
    assert [row["id"] for row in rows] == [option.id for option in question.options]
    assert rows[-1]["winner"]
