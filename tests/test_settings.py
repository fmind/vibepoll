"""Environment parsing at the process boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibepoll.settings import load_settings

_VARIABLES = ("PRESENTER_KEY", "GOOGLE_CLOUD_PROJECT", "PUBLIC_URL", "VIBEPOLL_CONFIG")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_usable_without_any_configuration() -> None:
    settings = load_settings()
    assert settings.public_url == "http://localhost:8080"
    assert settings.config_path == Path("poll.yaml")
    assert not settings.is_https
    assert not settings.use_firestore


def test_an_unset_presenter_key_is_generated_rather_than_left_empty() -> None:
    settings = load_settings()
    assert settings.generated_key
    assert len(settings.presenter_key) >= 8


def test_a_short_presenter_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESENTER_KEY", "short")
    with pytest.raises(ValueError, match="at least 8 characters"):
        load_settings()


def test_a_blank_required_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBEPOLL_CONFIG", "   ")
    with pytest.raises(ValueError, match="VIBEPOLL_CONFIG must not be blank"):
        load_settings()


def test_the_poll_definition_path_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBEPOLL_CONFIG", "/etc/vibepoll/night.yaml")
    assert load_settings().config_path == Path("/etc/vibepoll/night.yaml")


def test_a_trailing_slash_never_reaches_the_qr_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_URL", "https://poll.example/")
    assert load_settings().public_url == "https://poll.example"


def test_https_is_derived_from_the_public_url_not_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_URL", "https://poll.example")
    assert load_settings().is_https


def test_firestore_is_enabled_only_with_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-project")
    settings = load_settings()
    assert settings.use_firestore
    assert settings.project_id == "some-project"
