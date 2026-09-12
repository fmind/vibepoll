"""Poll definitions: what a good one loads to, and what a bad one refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vibepoll.config import PollConfig, load_config

_MINIMAL = """
id: demo
title: Demo
questions:
  - id: q1
    title: Pick one
    options:
      - id: a
        label: A
      - id: b
        label: B
"""

REPO_ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "poll.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_shipped_definition_is_valid() -> None:
    """The file the image actually deploys with, so a typo fails here not on stage."""
    config = load_config(REPO_ROOT / "poll.yaml")
    assert config.questions
    assert all(question.options for question in config.questions)


def test_a_minimal_definition_loads_with_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, _MINIMAL))
    assert config.id == "demo"
    assert config.subtitle == ""
    assert config.repo == ""
    assert config.panel_seconds == 5.0
    assert config.at(0) is not None
    assert config.at(1) is None
    assert config.question("q1") is not None
    assert config.question("nope") is None


def test_a_missing_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="poll definition not found"):
        load_config(tmp_path / "absent.yaml")


def test_malformed_yaml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not valid YAML"):
        load_config(write(tmp_path, "questions: [\n"))


def test_a_non_mapping_document_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must contain a YAML mapping"):
        load_config(write(tmp_path, "- just\n- a list\n"))


def test_a_schema_violation_names_the_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="questions"):
        load_config(write(tmp_path, "id: demo\ntitle: Demo\n"))


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"questions": []}, id="empty-deck"),
        pytest.param({"id": "has spaces"}, id="id-outside-alphabet"),
        pytest.param({"panel_seconds": 0}, id="zero-dwell"),
        pytest.param({"panel_seconds": -1}, id="negative-dwell"),
        pytest.param({"title": ""}, id="blank-title"),
        pytest.param({"repo": "javascript:alert(1)"}, id="non-https-repo"),
        pytest.param({"unexpected": "field"}, id="unknown-key"),
    ],
)
def test_invalid_definitions_are_refused(mutation: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "id": "demo",
        "title": "Demo",
        "questions": [{"id": "q1", "title": "T", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}],
    }
    payload.update(mutation)
    with pytest.raises(ValidationError):
        PollConfig.model_validate(payload)


def test_a_question_needs_at_least_two_options() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        PollConfig.model_validate(
            {"id": "d", "title": "D", "questions": [{"id": "q", "title": "T", "options": [{"id": "a", "label": "A"}]}]}
        )


def test_duplicate_option_ids_are_refused() -> None:
    """Two options sharing an id would make a ballot ambiguous."""
    with pytest.raises(ValueError, match="duplicate option id"):
        PollConfig.model_validate(
            {
                "id": "d",
                "title": "D",
                "questions": [
                    {"id": "q", "title": "T", "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]}
                ],
            }
        )


def test_duplicate_question_ids_are_refused() -> None:
    """Two questions sharing an id would silently share one ballot box."""
    question = {"id": "q", "title": "T", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}
    with pytest.raises(ValueError, match="duplicate question id"):
        PollConfig.model_validate({"id": "d", "title": "D", "questions": [question, question]})


def test_an_option_id_that_yaml_reads_as_a_boolean_is_reported(tmp_path: Path) -> None:
    """`id: yes` parses as True; the failure must name the field, not crash later."""
    body = _MINIMAL.replace("      - id: a\n", "      - id: yes\n")
    with pytest.raises(ValueError, match="not a valid poll definition"):
        load_config(write(tmp_path, body))
