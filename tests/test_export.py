"""Tests for the results exporter and HTML report generator."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.export import AAIF_LUXEMBOURG_METADATA, render_html_report

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_render_html_report_contains_event_and_results() -> None:
    data = {
        "event": AAIF_LUXEMBOURG_METADATA,
        "poll": {
            "id": "test-poll-1",
            "participants": 42,
            "total_ballots": 100,
            "voting_started_at": "2026-09-17T17:00:00Z",
            "voting_ended_at": "2026-09-17T17:30:00Z",
            "generated_at": "2026-09-18T04:00:00Z",
        },
        "questions": [
            {
                "id": "familiarity",
                "title": "Which best describes your experience with AI agents?",
                "subtitle": "Test subtitle",
                "type": "choice",
                "total_responses": 42,
                "options": [
                    {"id": "builder", "label": "Build agents", "count": 25, "percent": 59.5, "winner": True},
                    {"id": "user", "label": "Use agents", "count": 17, "percent": 40.5, "winner": False},
                ],
            },
            {
                "id": "message",
                "title": "Feedback",
                "subtitle": "Leave a message",
                "type": "text",
                "total_responses": 2,
                "messages": ["Great session!", "."],
            },
        ],
    }

    html = render_html_report(data)

    assert "<!doctype html>" in html
    assert "Agentic AI Night #1" in html
    assert "42" in html
    assert "Build agents" in html
    assert "Great session!" in html
    # "." is filtered as placeholder in substantive messages
    assert ">.<" not in html
    assert "downloadJSON" not in html
    assert "poll-data" not in html


def test_export_generates_valid_files(tmp_path: Path) -> None:
    data = {
        "event": AAIF_LUXEMBOURG_METADATA,
        "poll": {
            "id": "sample-poll",
            "participants": 53,
            "total_ballots": 403,
            "voting_started_at": "2026-09-17T16:54:47Z",
            "voting_ended_at": "2026-09-17T17:29:36Z",
            "generated_at": "2026-09-18T04:36:55Z",
        },
        "questions": [
            {
                "id": "aaif-project",
                "title": "Which AAIF project would you most like us to cover?",
                "subtitle": "Pick your favorite",
                "type": "choice",
                "total_responses": 53,
                "options": [
                    {
                        "id": "mcp",
                        "label": "Model Context Protocol (MCP)",
                        "count": 15,
                        "percent": 28.3,
                        "winner": True,
                    },
                ],
            }
        ],
    }

    json_path = tmp_path / "sample.json"
    html_path = tmp_path / "sample.html"

    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    html_path.write_text(render_html_report(data), encoding="utf-8")

    assert json_path.exists()
    assert html_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["poll"]["participants"] == 53
    assert payload["poll"]["total_ballots"] == 403
    assert len(payload["questions"]) == 1

    html_text = html_path.read_text(encoding="utf-8")
    assert "53" in html_text
    assert "403" not in html_text
    assert "Agentic AI Night #1" in html_text
    assert "Model Context Protocol (MCP)" in html_text
    assert "Speakers &amp; Organizing Team" in html_text
