"""Poll definition loaded from YAML at startup.

Content and pacing live in a file so the same image can run any event: the
environment describes the deployment, this file describes the poll. It is read
once and validated eagerly, so a typo fails the process with the offending field
path instead of surfacing as an empty screen in front of a room.

Option identifiers are stable keys. Rewording a label never re-keys ballots
already stored in Firestore; renaming an id does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = ["Option", "PollConfig", "Question", "load_config"]

# Ids travel in URLs, Firestore document names, and JSON keys, so they are kept
# to a conservative alphabet rather than sanitised at each boundary.
_ID = r"^[A-Za-z0-9_-]{1,64}$"
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Option(BaseModel):
    """One selectable answer."""

    model_config = _FROZEN

    id: Annotated[str, Field(pattern=_ID)]
    label: Annotated[str, Field(min_length=1, max_length=160)]


class Question(BaseModel):
    """One question with its closed answer set."""

    model_config = _FROZEN

    id: Annotated[str, Field(pattern=_ID)]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    subtitle: Annotated[str, Field(max_length=240)] = ""
    # Two is the fewest that makes a poll; ten bounds the choice list.
    # Check the actual question lengths at the venue's projector resolution.
    options: Annotated[tuple[Option, ...], Field(min_length=2, max_length=10)]

    def option_ids(self) -> frozenset[str]:
        return frozenset(option.id for option in self.options)

    @model_validator(mode="after")
    def _unique_option_ids(self) -> Self:
        # Pydantic validates each id in isolation; a repeat only shows up across
        # siblings, and it would make a ballot for this question ambiguous.
        if len(self.option_ids()) != len(self.options):
            msg = "duplicate option id"
            raise ValueError(msg)
        return self


class PollConfig(BaseModel):
    """A whole event: identity, pacing, and the deck."""

    model_config = _FROZEN

    id: Annotated[str, Field(pattern=_ID)]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    subtitle: Annotated[str, Field(max_length=120)] = ""
    repo: Annotated[str, Field(max_length=200, pattern=r"^(https://\S+)?$")] = ""
    panel_seconds: Annotated[float, Field(gt=0, le=300)] = 5.0
    questions: Annotated[tuple[Question, ...], Field(min_length=1, max_length=30)]

    def question(self, question_id: str) -> Question | None:
        """Return the question with this id, or ``None`` when unknown."""
        return next((question for question in self.questions if question.id == question_id), None)

    def at(self, index: int) -> Question | None:
        """Return the question at ``index``, or ``None`` when outside the deck."""
        if 0 <= index < len(self.questions):
            return self.questions[index]
        return None

    @model_validator(mode="after")
    def _unique_question_ids(self) -> Self:
        # Two questions sharing an id would silently share one ballot box.
        if len({question.id for question in self.questions}) != len(self.questions):
            msg = "duplicate question id"
            raise ValueError(msg)
        return self


def load_config(path: Path) -> PollConfig:
    """Read and validate the poll definition.

    Raises:
        ValueError: the file is missing, is not a YAML mapping, or does not
            satisfy the schema. The message names the path and the field so the
            fix is obvious without reading this module.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        msg = f"poll definition not found: {path}"
        raise ValueError(msg) from error
    except yaml.YAMLError as error:
        msg = f"{path} is not valid YAML: {error}"
        raise ValueError(msg) from error
    if not isinstance(raw, dict):
        msg = f"{path} must contain a YAML mapping, got {type(raw).__name__}"
        raise ValueError(msg)
    try:
        return PollConfig.model_validate(raw)
    except ValidationError as error:
        msg = f"{path} is not a valid poll definition:\n{error}"
        raise ValueError(msg) from error
