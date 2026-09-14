"""Shared fixtures: every test runs against an in-memory repository, offline.

Backend tests use a small synthetic deck so rewording questions does not break
behaviour checks. The config test validates the shipped YAML, and browser tests
exercise the real eight-question deck, including its projector layout.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from vibepoll.app import create_app
from vibepoll.config import PollConfig
from vibepoll.settings import Settings
from vibepoll.store import MemoryRepository

PRESENTER_KEY = "test-presenter-key"


def make_config(questions: int = 3, options: int = 4) -> PollConfig:
    """Build a deck of ``questions`` questions with ``options`` answers each."""
    return PollConfig.model_validate(
        {
            "id": "test-poll",
            "title": "Agentic AI Night #1",
            "subtitle": "AAIF Luxembourg Launch",
            "repo": "https://github.com/fmind/vibepoll",
            "panel_seconds": 3,
            "questions": [
                {
                    "id": f"q{index}",
                    "title": f"Question {index}?",
                    "subtitle": f"Subtitle {index}",
                    "options": [{"id": f"o{position}", "label": f"Option {position}"} for position in range(options)],
                }
                for index in range(questions)
            ],
        }
    )


@pytest.fixture
def config() -> PollConfig:
    return make_config()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        config_path=Path("unused.yaml"),
        presenter_key=PRESENTER_KEY,
        project_id=None,
        public_url="https://poll.example",
        generated_key=False,
    )


@pytest.fixture
def repository() -> MemoryRepository:
    return MemoryRepository()


@pytest.fixture
def app(settings: Settings, config: PollConfig, repository: MemoryRepository) -> Litestar:
    return create_app(settings=settings, config=config, repository=repository)


# The presenter cookie is issued with `Secure`, matching the https PUBLIC_URL
# of a real deployment. The test transport must therefore also speak https, or
# the client silently drops the cookie and every presenter test fails for a
# reason that would never occur in production.
BASE_URL = "https://poll.example"


@pytest.fixture
def client(app: Litestar) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=app, base_url=BASE_URL) as test_client:
        yield test_client


@pytest.fixture
def presenter(app: Litestar) -> Iterator[TestClient[Litestar]]:
    """A client already holding the presenter cookie."""
    with TestClient(app=app, base_url=BASE_URL) as test_client:
        response = test_client.get("/present", params={"key": PRESENTER_KEY})
        assert response.status_code == 200
        yield test_client
