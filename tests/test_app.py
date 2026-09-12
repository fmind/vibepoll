"""HTTP contract: access control, ballot validation, and the SSE stream."""

from __future__ import annotations

from litestar import Litestar
from litestar.testing import TestClient

from tests.conftest import BASE_URL, PRESENTER_KEY
from vibepoll.config import PollConfig

VOTER = "voter0000000001"


def test_vote_page_is_public(client: TestClient[Litestar]) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Agentic AI Night #1" in response.text


def test_health_reports_ok(client: TestClient[Litestar]) -> None:
    """Served at /health, not /healthz: Google Frontend swallows that path."""
    assert client.get("/health").text == "ok"
    assert client.get("/healthz").status_code == 404


def test_qr_code_encodes_the_public_url(client: TestClient[Litestar]) -> None:
    response = client.get("/qr.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.lstrip().startswith("<svg")


def test_security_headers_are_applied(client: TestClient[Litestar]) -> None:
    headers = client.get("/").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "'unsafe-inline'" not in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    # PUBLIC_URL is https in the fixture, so HSTS must be asserted even though
    # the test transport itself is not TLS.
    assert headers["strict-transport-security"].startswith("max-age=31536000")


def test_presenter_console_is_hidden_without_the_key(client: TestClient[Litestar]) -> None:
    assert client.get("/present").status_code == 404


def test_presenter_console_rejects_a_wrong_key(client: TestClient[Litestar]) -> None:
    assert client.get("/present", params={"key": "not-the-key"}).status_code == 404


def test_presenter_key_is_exchanged_for_a_hardened_cookie(app: Litestar) -> None:
    with TestClient(app=app, base_url=BASE_URL) as client:
        response = client.get("/present", params={"key": PRESENTER_KEY})
        assert response.status_code == 200
        header = "".join(response.history[0].headers.get_list("set-cookie"))
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "SameSite=strict" in header.replace("samesite", "SameSite")


def test_control_is_refused_without_the_presenter_cookie(client: TestClient[Litestar]) -> None:
    assert client.post("/api/control", json={"action": "open"}).status_code == 404


def test_control_rejects_an_unknown_action(presenter: TestClient[Litestar]) -> None:
    assert presenter.post("/api/control", json={"action": "rm -rf"}).status_code == 400


def test_any_question_accepts_a_ballot_during_the_slideshow(client: TestClient[Litestar], config: PollConfig) -> None:
    """No presenter cookie needed: the room votes at its own pace."""
    for question in config.questions:
        payload = {"question": question.id, "option": question.options[0].id, "voter": VOTER}
        assert client.post("/api/vote", json=payload).status_code == 200


def test_vote_is_refused_once_the_presenter_takes_the_stage(
    presenter: TestClient[Litestar], config: PollConfig
) -> None:
    presenter.post("/api/control", json={"action": "review"})
    question = config.questions[0]
    payload = {"question": question.id, "option": question.options[0].id, "voter": VOTER}
    assert presenter.post("/api/vote", json=payload).status_code == 400


def test_vote_rejects_an_unknown_question(client: TestClient[Litestar]) -> None:
    payload = {"question": "not-a-question", "option": "yes", "voter": VOTER}
    assert client.post("/api/vote", json=payload).status_code == 400


def test_poll_endpoint_serves_the_deck_and_its_pacing(client: TestClient[Litestar], config: PollConfig) -> None:
    payload = client.get("/api/poll").json()
    assert [item["id"] for item in payload["questions"]] == [question.id for question in config.questions]
    assert payload["panelSeconds"] == config.panel_seconds
    assert payload["questions"][0]["title"]
    assert payload["questions"][0]["options"][0]["label"]


def test_vote_rejects_a_malformed_voter_identifier(client: TestClient[Litestar], config: PollConfig) -> None:
    question = config.questions[0]
    payload = {"question": question.id, "option": question.options[0].id, "voter": "../../etc/passwd"}
    assert client.post("/api/vote", json=payload).status_code == 400


def test_qr_code_is_a_standalone_svg_document(client: TestClient[Litestar]) -> None:
    """An <img src> silently falls back to alt text if the xmlns is missing."""
    body = client.get("/qr.svg").text
    assert 'xmlns="http://www.w3.org/2000/svg"' in body
    assert "viewBox=" in body


def test_a_non_ascii_key_is_a_404_not_a_crash(client: TestClient[Litestar]) -> None:
    """compare_digest raises on non-ASCII text; a junk key must stay indistinguishable."""
    assert client.get("/present", params={"key": "e" * 7 + "\u00e9"}).status_code == 404


def test_the_stage_screen_shows_the_repository(presenter: TestClient[Litestar], config: PollConfig) -> None:
    assert config.repo in presenter.get("/present").text
