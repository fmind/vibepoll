"""Real browser regressions against a local, memory-only server."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, Route, expect, sync_playwright

_LOCAL_KEY = "browser-test-presenter"


@pytest.fixture(scope="session")
def browser_server() -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {key: value for key, value in os.environ.items() if key != "GOOGLE_CLOUD_PROJECT"}
    env.update(PRESENTER_KEY=_LOCAL_KEY, PUBLIC_URL=url, VIBEPOLL_CONFIG=str(Path("poll.yaml").resolve()))
    with subprocess.Popen(  # noqa: S603 - fixed local test server, no shell
        [
            sys.executable,
            "-m",
            "granian",
            "--interface",
            "asgi",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "vibepoll.app:create_app",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as server:
        try:
            with httpx.Client(base_url=url, timeout=1) as client:
                for _ in range(100):
                    if server.poll() is not None:
                        pytest.fail("browser test server exited during startup")
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("browser test server did not become ready")
            yield url
        finally:
            server.terminate()
            server.wait(timeout=10)


@pytest.fixture
def phone(browser_server: str) -> Iterator[Page]:
    with httpx.Client(base_url=browser_server, follow_redirects=True) as client:
        client.get("/present", params={"key": _LOCAL_KEY})
        assert client.post("/api/control", json={"action": "clear"}).status_code == 200
    # A session-long sync Playwright fixture keeps its event loop active across
    # pytest-asyncio tests. Close it after each browser test instead.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = context.new_page()
            page.goto(browser_server)
            expect(page.get_by_role("radio").first).to_be_visible()
            yield page
        finally:
            browser.close()


def test_a_failed_vote_stays_on_the_question_and_can_be_retried(phone: Page) -> None:
    title = phone.locator("#question-title").inner_text()
    phone.route("**/api/vote", lambda route: route.fulfill(status=503, body="unavailable"))
    phone.get_by_role("radio").first.click()
    expect(phone.locator("#vote-status")).to_contain_text("Couldn't confirm")
    expect(phone.locator("#question-title")).to_have_text(title)
    expect(phone.locator("#progress")).to_have_text("Question 1 of 6")
    phone.unroute("**/api/vote")
    phone.get_by_role("radio").first.click()
    expect(phone.locator("#progress")).to_have_text("Question 2 of 6")
    expect(phone.locator("#question-title")).to_be_focused()


def test_other_voters_do_not_remove_keyboard_focus(phone: Page, browser_server: str) -> None:
    radio = phone.get_by_role("radio").nth(1)
    radio.focus()
    response = phone.request.post(
        f"{browser_server}/api/vote", data={"question": "familiarity", "option": "builder", "voter": "another-browser"}
    )
    assert response.ok
    # Let the actual EventSource deliver the other participant's update.
    phone.wait_for_timeout(200)
    expect(radio).to_be_focused()
    phone.keyboard.press("ArrowDown")
    expect(phone.locator("#progress")).to_have_text("Question 2 of 6")
    expect(phone.locator("#question-title")).to_be_focused()


def test_completion_waits_for_confirmation_and_review_has_a_done_action(phone: Page) -> None:
    for index in range(5):
        phone.get_by_role("radio").first.click()
        expect(phone.locator("#progress")).to_have_text(f"Question {index + 2} of 6")
    held: list[Route] = []
    phone.route("**/api/vote", lambda route: held.append(route))
    phone.get_by_role("radio").first.click()
    expect(phone.locator("#vote-status")).to_have_text("Saving…")
    phone.wait_for_timeout(350)
    expect(phone.locator("#stage-done")).to_be_hidden()
    expect(phone.locator("#progress")).to_have_text("Question 6 of 6")
    assert len(held) == 1
    held[0].continue_()
    expect(phone.locator("#stage-done")).to_be_visible()
    phone.get_by_role("button", name="Change an answer").click()
    for _ in range(5):
        phone.get_by_role("button", name="Next →", exact=True).click()
    phone.get_by_role("button", name="Done", exact=True).click()
    expect(phone.locator("#stage-done")).to_be_visible()
    phone.reload()
    expect(phone.locator("#stage-done")).to_be_visible()


def test_projector_steps_review_and_finish_fit_the_screen(phone: Page, browser_server: str) -> None:
    stage = phone.context.new_page()
    stage.set_viewport_size({"width": 1280, "height": 720})
    stage.goto(f"{browser_server}/present?key={_LOCAL_KEY}")
    expect(stage.locator("#panel-welcome")).to_be_visible()
    for index in range(6):
        stage.locator('[data-step="1"]').click()
        expect(stage.locator("#a-position")).to_have_text(f"[{index + 1}/6]")
        expect(phone.locator("#stage-voting")).to_be_visible()
        assert stage.evaluate("document.documentElement.scrollHeight <= innerHeight")
        for label in stage.locator("#a-bars .bar-label").all():
            assert label.evaluate(
                "node => node.getBoundingClientRect().height <= node.parentElement.getBoundingClientRect().height"
            )
    stage.get_by_role("button", name="Review", exact=True).click()
    expect(phone.locator("#stage-closed")).to_be_visible()
    assert stage.locator(".bar-key").count() == 0
    for index in range(6):
        expect(stage.locator("#q-position")).to_have_text(f"[{index + 1}/6]")
        assert stage.evaluate("document.documentElement.scrollHeight <= innerHeight")
        if index < 5:
            stage.keyboard.press("ArrowRight")
    stage.get_by_role("button", name="Finish", exact=True).click()
    expect(stage.get_by_role("heading", name="Thank you.", exact=True)).to_be_visible()
    assert stage.evaluate("document.documentElement.scrollHeight <= innerHeight")
    expect(phone.locator("#closed-title")).to_have_text("That's a wrap.")
    stage.close()
