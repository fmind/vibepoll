#!/usr/bin/env python3
"""Export vibepoll results to structured JSON and a standalone HTML report.

Fetches ballots from Firestore (or reads a saved JSON), calculates question
tallies, and produces both a machine-readable JSON document and a styled,
self-contained HTML report that can be shared with attendees or published.

Usage:
    uv run python scripts/export.py [--project aaif-vibepoll] [--output-dir results]
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.cloud.firestore_v1 import AsyncClient

from vibepoll.config import PollConfig, load_config
from vibepoll.firestore import FirestoreRepository
from vibepoll.store import Poll

DEFAULT_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "aaif-vibepoll")
DEFAULT_CONFIG = Path("poll.yaml")
DEFAULT_OUTPUT_DIR = Path("results")

# Event metadata preset for the inaugural AAIF Luxembourg event
AAIF_LUXEMBOURG_METADATA: dict[str, Any] = {
    "id": "aaif-luxembourg-1",
    "title": "Agentic AI Night #1 — AAIF Luxembourg Launch",
    "short_title": "Agentic AI Night #1",
    "subtitle": "AAIF Luxembourg Launch",
    "date": "2026-09-17",
    "date_formatted": "Thursday, September 17, 2026",
    "time": "18:00 - 20:00 CEST",
    "venue": "University of Luxembourg, Kirchberg Campus",
    "address": "29 Av. John F. Kennedy, 1855 Kirchberg Luxembourg",
    "luma_url": "https://luma.com/4ciica7u",
    "repo_url": "https://github.com/fmind/vibepoll",
    "aaif_url": "https://aaif.io/",
    "chapter": "AAIF Community Luxembourg",
    "keynote": {
        "speaker": "Maxime Cordy",
        "role": "Senior Research Scientist & Professor at SnT, University of Luxembourg",
        "title": "How Reliable Are Your AI Agents? And How Research Can Help",
    },
    "organizers": [
        {"name": "Médéric Hurier (Fmind)", "role": "Lead AI Architect, Fmind.dev"},
        {"name": "Maxime Cordy", "role": "Senior Scientist & Professor, SnT (Uni.lu)"},
        {"name": "Mustafa Aldemir", "role": "Head of Artificial Intelligence, ArcelorMittal"},
        {"name": "Hazal Kantarci", "role": "Partner, AI & Data Science Transformation Leader, PwC Luxembourg"},
        {"name": "Hajar Khizou", "role": "Data Engineering & Science Manager, SustainCERT"},
        {"name": "Max Amordeluso", "role": "Data & AI Leader, Europe North, AWS Luxembourg"},
        {"name": "Irina Neagu Muceli", "role": "Sr Cloud & AI Specialist, Microsoft"},
        {"name": "Antoine Leffondré", "role": "Representative Google Cloud Luxembourg"},
    ],
}


def resolve_metadata(config: PollConfig) -> dict[str, Any]:
    """Return event metadata, using the AAIF preset for that poll id or generic values."""
    if config.id.startswith("aaif-luxembourg-1"):
        return AAIF_LUXEMBOURG_METADATA
    return {
        "id": config.id,
        "title": config.title,
        "short_title": config.title,
        "subtitle": config.subtitle,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "date_formatted": datetime.now(UTC).strftime("%A, %B %d, %Y"),
        "time": "",
        "venue": "",
        "address": "",
        "luma_url": "",
        "repo_url": config.repo,
        "aaif_url": "",
        "chapter": "Community Poll",
        "organizers": [],
    }


async def fetch_results(project_id: str, poll_id: str, config: PollConfig) -> dict[str, Any]:
    """Retrieve raw ballots from Firestore and compute tallies."""
    repo = FirestoreRepository(project_id=project_id, poll_id=poll_id)
    poll = Poll(repo, config)
    await poll.restore()

    # Query timestamps directly from firestore
    firestore_client = AsyncClient(project=project_id)
    votes_ref = firestore_client.collection("polls").document(poll_id).collection("votes")

    timestamps: list[datetime] = []
    async for doc in votes_ref.stream():
        doc_data = doc.to_dict() or {}
        at = doc_data.get("at")
        if isinstance(at, datetime):
            timestamps.append(at)

    firestore_client.close()

    timestamps.sort()
    start_time = timestamps[0].isoformat() if timestamps else None
    end_time = timestamps[-1].isoformat() if timestamps else None

    questions_data: list[dict[str, Any]] = []
    for question in config.questions:
        res = poll.results(question)
        q_dict: dict[str, Any] = {
            "id": question.id,
            "title": question.title,
            "subtitle": question.subtitle,
            "type": question.type,
            "total_responses": res["total"],
        }
        if question.type == "choice":
            opt_map = {opt.id: opt.label for opt in question.options}
            q_dict["options"] = [
                {
                    "id": row["id"],
                    "label": opt_map.get(row["id"], row["id"]),
                    "count": row["count"],
                    "percent": row["percent"],
                    "winner": row["winner"],
                }
                for row in res["rows"]
            ]
        elif question.type == "text":
            q_dict["messages"] = res.get("messages", [])
        questions_data.append(q_dict)

    participants = poll.participants()
    total_ballots = len(timestamps) if timestamps else sum(q["total_responses"] for q in questions_data)

    await poll.close()

    return {
        "event": resolve_metadata(config),
        "poll": {
            "id": poll_id,
            "participants": participants,
            "total_ballots": total_ballots,
            "voting_started_at": start_time,
            "voting_ended_at": end_time,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "questions": questions_data,
    }


def render_html_report(data: dict[str, Any]) -> str:
    """Generate a self-contained, accessible, responsive HTML report."""
    event = data["event"]
    poll = data["poll"]
    questions = data["questions"]

    # Calculate key summary stats
    fam_q = next((q for q in questions if q["id"] == "familiarity"), None)
    builder_pct = 0.0
    if fam_q and fam_q.get("options"):
        for opt in fam_q["options"]:
            if opt["id"] == "builder":
                builder_pct = opt["percent"]

    # Process audience messages
    message_q = next((q for q in questions if q["id"] == "message"), None)
    raw_messages: list[str] = message_q.get("messages", []) if message_q else []
    # Classify meaningful feedback vs brief tests/placeholders
    placeholders = {".", "na", "n/a", "none", "/", "test"}
    substantive_messages = [m for m in raw_messages if m.strip().lower() not in placeholders and len(m.strip()) > 2]

    # Build question sections HTML
    questions_html = []
    for idx, q in enumerate(questions, start=1):
        q_id = html.escape(q["id"])
        q_title = html.escape(q["title"])
        q_subtitle = html.escape(q.get("subtitle", ""))
        total_resp = q["total_responses"]

        if q["type"] == "choice":
            rows_html = []
            for opt in q["options"]:
                label = html.escape(opt["label"])
                count = opt["count"]
                percent = opt["percent"]
                winner = opt["winner"]
                winner_attr = ' data-winner="true"' if winner else ""
                winner_badge = '<span class="winner-badge" title="Leading Choice">★ Leader</span>' if winner else ""

                rows_html.append(
                    f"""              <li class="bar-row"{winner_attr} style="--pct: {percent}%;">
                <div class="bar-fill"></div>
                <div class="bar-content">
                  <span class="bar-label">{label}{winner_badge}</span>
                  <span class="bar-value"><strong>{count}</strong> <small>({percent:.1f}%)</small></span>
                </div>
              </li>"""
                )

            bars_markup = "\n".join(rows_html)
            questions_html.append(
                f"""        <article class="question-card" id="q-{q_id}">
          <header class="card-header">
            <div class="card-eyebrow">
              <span class="q-badge">Question {idx} of {len(questions)}</span>
              <span class="resp-count">{total_resp} responses</span>
            </div>
            <h3 class="card-title">{q_title}</h3>
            {f'<p class="card-subtitle">{q_subtitle}</p>' if q_subtitle else ""}
          </header>
          <ul class="bars-list">
{bars_markup}
          </ul>
        </article>"""
            )
        elif q["type"] == "text":
            msg_cards = []
            for m in substantive_messages:
                clean_m = html.escape(m)
                msg_cards.append(f"""            <div class="message-card">
              <span class="quote-mark">“</span>
              <p class="message-text">{clean_m}</p>
            </div>""")
            cards_markup = "\n".join(msg_cards)
            questions_html.append(
                f"""        <article class="question-card text-question" id="q-{q_id}">
          <header class="card-header">
            <div class="card-eyebrow">
              <span class="q-badge">Question {idx} of {len(questions)} · Free Text</span>
              <span class="resp-count">{total_resp} responses ({len(substantive_messages)} featured)</span>
            </div>
            <h3 class="card-title">{q_title}</h3>
            {f'<p class="card-subtitle">{q_subtitle}</p>' if q_subtitle else ""}
          </header>
          <div class="messages-grid">
{cards_markup}
          </div>
        </article>"""
            )

    all_questions_markup = "\n\n".join(questions_html)

    # Organizers markup
    org_chips = []
    for org in event.get("organizers", []):
        org_name = html.escape(org["name"])
        org_role = html.escape(org["role"])
        org_chips.append(
            f"""        <div class="speaker-card">
          <div class="speaker-avatar">{org_name[0]}</div>
          <div>
            <strong>{org_name}</strong>
            <span>{org_role}</span>
          </div>
        </div>"""
        )
    org_markup = "\n".join(org_chips)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="description" content="Official audience poll results from Agentic AI Night #1 — AAIF Luxembourg Launch on September 17, 2026." />
  <title>{html.escape(event["title"])} — Poll Results</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>🗳️</text></svg>" />
  <style>
    /* Fmind palette (github.com/fmind/theme) */
    :root {{
      --bg: #FFFFFF;
      --surface: #F8F9FA;
      --surface-card: #FFFFFF;
      --surface-2: #E8F0FE;
      --line: #DADCE0;
      --line-subtle: #E8EAED;
      --text: #202124;
      --muted: #5F6368;
      --accent: #1A73E8;
      --accent-soft: #D2E3FC;
      --accent-dark: #174EA6;
      --success: #137333;
      --success-soft: #CEEAD6;
      --success-dark: #0D652D;
      --radius: 14px;
      --radius-sm: 8px;
      --shadow-sm: 0 1px 2px rgba(60,64,67,0.1), 0 1px 3px rgba(60,64,67,0.06);
      --shadow-md: 0 4px 12px rgba(60,64,67,0.08), 0 1px 3px rgba(60,64,67,0.05);
      --font: "Google Sans", system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      --font-mono: "Google Sans Code", "GoogleSansCode Nerd Font Mono", monospace, monospace;
      color-scheme: light;
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #121314;
        --surface: #1E1F21;
        --surface-card: #28292A;
        --surface-2: #172A46;
        --line: #3C4043;
        --line-subtle: #2D3134;
        --text: #E8EAED;
        --muted: #9AA0A6;
        --accent: #8AB4F8;
        --accent-soft: #17325B;
        --accent-dark: #AECBFA;
        --success: #81C995;
        --success-soft: #133924;
        --success-dark: #A8DAB5;
        --shadow-sm: 0 1px 2px rgba(0,0,0,0.3);
        --shadow-md: 0 4px 12px rgba(0,0,0,0.4);
        color-scheme: dark;
      }}
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      padding: 0;
    }}

    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    .container {{
      max-width: 960px;
      margin: 0 auto;
      padding: 0 1.5rem;
    }}

    /* Top Navigation Header */
    .site-nav {{
      border-bottom: 1px solid var(--line-subtle);
      background: var(--surface);
      padding: 0.85rem 0;
      position: sticky;
      top: 0;
      z-index: 50;
      backdrop-filter: blur(8px);
    }}

    .nav-inner {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
    }}

    .brand-tag {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-weight: 700;
      font-size: 0.95rem;
      color: var(--text);
    }}

    .brand-badge {{
      background: var(--accent-soft);
      color: var(--accent-dark);
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 800;
      letter-spacing: 0.04em;
    }}

    .nav-actions {{
      display: flex;
      gap: 0.6rem;
      align-items: center;
    }}

    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.45rem 0.85rem;
      border-radius: var(--radius-sm);
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      border: 1px solid var(--line);
      background: var(--surface-card);
      color: var(--text);
      transition: all 0.15s ease-in-out;
    }}

    .btn:hover {{
      border-color: var(--accent);
      color: var(--accent);
      text-decoration: none;
    }}

    .btn-primary {{
      background: var(--accent);
      color: #FFFFFF;
      border-color: var(--accent);
    }}

    .btn-primary:hover {{
      background: var(--accent-dark);
      color: #FFFFFF;
      border-color: var(--accent-dark);
    }}

    /* Hero Banner */
    .hero {{
      padding: 3rem 0 2rem;
      border-bottom: 1px solid var(--line-subtle);
      background: linear-gradient(180deg, var(--surface) 0%, var(--bg) 100%);
    }}

    .hero-eyebrow {{
      color: var(--accent);
      font-weight: 800;
      font-size: 0.8rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 0.5rem;
    }}

    .hero-title {{
      font-size: clamp(2rem, 4.5vw, 2.75rem);
      font-weight: 800;
      line-height: 1.15;
      letter-spacing: -0.03em;
      margin-bottom: 0.5rem;
    }}

    .hero-subtitle {{
      font-size: clamp(1.1rem, 2vw, 1.35rem);
      color: var(--muted);
      margin-bottom: 1.75rem;
    }}

    .meta-pills {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
    }}

    .meta-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.35rem 0.75rem;
      background: var(--surface-card);
      border: 1px solid var(--line);
      border-radius: 100px;
      font-size: 0.85rem;
      color: var(--text);
      box-shadow: var(--shadow-sm);
    }}

    /* Key Insights Grid */
    .summary-section {{
      padding: 2.5rem 0 1.5rem;
    }}

    .section-title {{
      font-size: 1.4rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      margin-bottom: 1.25rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}

    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
    }}

    .summary-card {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      box-shadow: var(--shadow-sm);
    }}

    .summary-stat {{
      font-size: 1.85rem;
      font-weight: 800;
      color: var(--accent);
      line-height: 1.1;
    }}

    .summary-label {{
      font-weight: 700;
      font-size: 0.95rem;
    }}

    .summary-desc {{
      font-size: 0.85rem;
      color: var(--muted);
      line-height: 1.4;
    }}

    /* Jump Navigation */
    .jump-nav {{
      margin: 1.5rem 0 2rem;
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      align-items: center;
    }}

    .jump-label {{
      font-size: 0.8rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-right: 0.2rem;
    }}

    .jump-chip {{
      font-size: 0.8rem;
      padding: 0.25rem 0.6rem;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 100px;
      color: var(--text);
    }}

    .jump-chip:hover {{
      background: var(--surface-2);
      border-color: var(--accent);
      text-decoration: none;
    }}

    /* Question Cards */
    .questions-list {{
      display: flex;
      flex-direction: column;
      gap: 2rem;
      margin-bottom: 3rem;
    }}

    .question-card {{
      background: var(--surface-card);
      border: 1.5px solid var(--line);
      border-radius: var(--radius);
      padding: 1.75rem;
      box-shadow: var(--shadow-sm);
      scroll-margin-top: 5rem;
    }}

    .card-header {{
      margin-bottom: 1.35rem;
    }}

    .card-eyebrow {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.4rem;
    }}

    .q-badge {{
      font-size: 0.75rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--accent);
    }}

    .resp-count {{
      font-size: 0.8rem;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }}

    .card-title {{
      font-size: 1.3rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      line-height: 1.25;
      margin-bottom: 0.35rem;
    }}

    .card-subtitle {{
      font-size: 0.9rem;
      color: var(--muted);
      line-height: 1.4;
    }}

    /* Bars */
    .bars-list {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
    }}

    .bar-row {{
      position: relative;
      border: 1.5px solid var(--line);
      border-radius: 10px;
      background: var(--surface);
      overflow: hidden;
      min-height: 3.2rem;
      display: flex;
      align-items: center;
      isolation: isolate;
      transition: transform 0.1s ease;
    }}

    .bar-fill {{
      position: absolute;
      inset: 0 auto 0 0;
      width: var(--pct, 0%);
      background: var(--surface-2);
      z-index: 1;
      transition: width 0.6s cubic-bezier(0.22, 1, 0.36, 1);
    }}

    .bar-row[data-winner="true"] {{
      border-color: var(--success);
    }}

    .bar-row[data-winner="true"] .bar-fill {{
      background: var(--success-soft);
    }}

    .bar-content {{
      position: relative;
      z-index: 2;
      width: 100%;
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0.75rem 1rem;
      gap: 1rem;
    }}

    .bar-label {{
      font-size: 0.95rem;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }}

    .winner-badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.2rem;
      background: var(--success);
      color: #FFFFFF;
      font-size: 0.7rem;
      font-weight: 800;
      padding: 0.15rem 0.45rem;
      border-radius: 100px;
      letter-spacing: 0.02em;
    }}

    .bar-value {{
      font-size: 1.05rem;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      text-align: right;
    }}

    .bar-value strong {{
      font-weight: 800;
    }}

    .bar-value small {{
      color: var(--muted);
      font-size: 0.85rem;
      margin-left: 0.2rem;
    }}

    .bar-row[data-winner="true"] .bar-value strong {{
      color: var(--success);
    }}

    /* Messages Grid */
    .messages-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 0.85rem;
    }}

    .message-card {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      padding: 1rem 1.15rem;
      display: flex;
      gap: 0.6rem;
      align-items: flex-start;
    }}

    .quote-mark {{
      font-size: 1.6rem;
      line-height: 1;
      color: var(--accent);
      font-family: Georgia, serif;
      opacity: 0.7;
    }}

    .message-text {{
      font-size: 0.9rem;
      color: var(--text);
      line-height: 1.45;
    }}

    /* Event Context & Team */
    .event-context {{
      background: var(--surface);
      border-radius: var(--radius);
      padding: 2rem;
      border: 1px solid var(--line);
      margin-bottom: 3rem;
    }}

    .speakers-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 0.75rem;
      margin-top: 1rem;
    }}

    .speaker-card {{
      background: var(--surface-card);
      border: 1px solid var(--line-subtle);
      border-radius: var(--radius-sm);
      padding: 0.75rem 0.9rem;
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}

    .speaker-avatar {{
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: var(--accent-soft);
      color: var(--accent-dark);
      font-weight: 800;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.85rem;
      flex-shrink: 0;
    }}

    .speaker-card div {{
      display: flex;
      flex-direction: column;
      line-height: 1.25;
    }}

    .speaker-card strong {{
      font-size: 0.88rem;
    }}

    .speaker-card span {{
      font-size: 0.76rem;
      color: var(--muted);
    }}

    /* Footer */
    .site-footer {{
      border-top: 1px solid var(--line-subtle);
      padding: 2.5rem 0;
      color: var(--muted);
      font-size: 0.85rem;
    }}

    .footer-inner {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 1rem;
      align-items: center;
    }}

    /* Print styles */
    @media print {{
      .site-nav, .jump-nav, .nav-actions, .btn {{ display: none !important; }}
      body {{ background: #FFF !important; color: #000 !important; }}
      .question-card {{ break-inside: avoid; border: 1px solid #CCC; box-shadow: none; }}
      .bar-fill {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
    }}
  </style>
</head>
<body>
  <!-- Navigation Header -->
  <nav class="site-nav">
    <div class="container nav-inner">
      <div class="brand-tag">
        <span>🗳️ vibepoll</span>
        <span class="brand-badge">AAIF Luxembourg</span>
      </div>
      <div class="nav-actions">
        <a href="{html.escape(event["luma_url"])}" class="btn" target="_blank" rel="noopener noreferrer">Luma Event ↗</a>
        <a href="{html.escape(event["repo_url"])}" class="btn" target="_blank" rel="noopener noreferrer">GitHub ↗</a>
      </div>
    </div>
  </nav>

  <!-- Hero Header -->
  <header class="hero">
    <div class="container">
      <p class="hero-eyebrow">Official Event Survey Results</p>
      <h1 class="hero-title">{html.escape(event["title"])}</h1>
      <p class="hero-subtitle">Conducted live during the networking break on September 17, 2026</p>

      <div class="meta-pills">
        <span class="meta-pill">📅 {html.escape(event["date_formatted"])}</span>
        <span class="meta-pill">📍 {html.escape(event["venue"])}</span>
        <span class="meta-pill">👥 <strong>{poll["participants"]}</strong> live voters</span>
        <span class="meta-pill">⚡ Powered by <a href="{html.escape(event["repo_url"])}" target="_blank">vibepoll</a></span>
      </div>
    </div>
  </header>

  <main class="container">
    <!-- Executive Summary / Takeaways -->
    <section class="summary-section">
      <h2 class="section-title">📊 Executive Highlights</h2>
      <div class="summary-grid">
        <div class="summary-card">
          <span class="summary-stat">{builder_pct:.1f}%</span>
          <span class="summary-label">Build AI Agents</span>
          <p class="summary-desc">Combined with 32.1% regular users, nearly 80% of the room actively uses agents in their daily workflows.</p>
        </div>

        <div class="summary-card">
          <span class="summary-stat">50.9%</span>
          <span class="summary-label">Coding & Testing Use Case</span>
          <p class="summary-desc">Software development is the #1 application, followed by research & analysis at 34.0%.</p>
        </div>

        <div class="summary-card">
          <span class="summary-stat">71.7%</span>
          <span class="summary-label">Require Human Verification</span>
          <p class="summary-desc">43.4% require step-by-step approval and 28.3% review diffs; only 5.7% trust fully autonomous execution.</p>
        </div>

        <div class="summary-card">
          <span class="summary-stat">22.6%</span>
          <span class="summary-label">Top Barrier: Security & Reliability</span>
          <p class="summary-desc">Security/privacy (22.6%), reliability (20.8%), and integration (20.8%) represent the core blockers.</p>
        </div>

        <div class="summary-card">
          <span class="summary-stat">41.5%</span>
          <span class="summary-label">Want "Building Agents" Next</span>
          <p class="summary-desc">The strongest demand for Event #2 is hands-on agent building, followed by testing & evaluation (15.1%).</p>
        </div>

        <div class="summary-card">
          <span class="summary-stat">28.3%</span>
          <span class="summary-label">Most Desired Project: MCP</span>
          <p class="summary-desc">Model Context Protocol led foundation interest, followed by Agent2Agent (17.0%) and AGENTS.md (15.1%).</p>
        </div>
      </div>
    </section>

    <!-- Quick Jump Chips -->
    <div class="jump-nav">
      <span class="jump-label">Jump to:</span>
      <a href="#q-familiarity" class="jump-chip">1. Experience</a>
      <a href="#q-role" class="jump-chip">2. Role</a>
      <a href="#q-usecase" class="jump-chip">3. Use Case</a>
      <a href="#q-autonomy" class="jump-chip">4. Supervision</a>
      <a href="#q-blocker" class="jump-chip">5. Barriers</a>
      <a href="#q-topic" class="jump-chip">6. Next Topics</a>
      <a href="#q-aaif-project" class="jump-chip">7. AAIF Projects</a>
      <a href="#q-message" class="jump-chip">8. Audience Feedback</a>
    </div>

    <!-- Question Cards List -->
    <section class="questions-list">
{all_questions_markup}
    </section>

    <!-- Event Context & Organizing Team -->
    <section class="event-context">
      <h2 class="section-title">🏛️ About the Event</h2>
      <p style="margin-bottom: 1rem; color: var(--muted); font-size: 0.95rem;">
        This was the launch gathering of the <strong>Agentic AI Foundation (AAIF) Luxembourg Chapter</strong>, hosted at the University of Luxembourg Kirchberg Campus.
        The evening featured the introduction of the local chapter, an interactive live poll with <strong>vibepoll</strong>, and a keynote talk by <strong>Maxime Cordy</strong> on <em>"How Reliable Are Your AI Agents? And How Research Can Help"</em>.
      </p>

      <h3 style="font-size: 1rem; font-weight: 700; margin-top: 1.25rem;">Speakers &amp; Organizing Team</h3>
      <div class="speakers-grid">
{org_markup}
      </div>
    </section>
  </main>

  <!-- Footer -->
  <footer class="site-footer">
    <div class="container footer-inner">
      <p>© 2026 {html.escape(event["chapter"])} · <a href="{html.escape(event["aaif_url"])}" target="_blank">Agentic AI Foundation</a></p>
      <p>Source code on <a href="{html.escape(event["repo_url"])}" target="_blank">GitHub (fmind/vibepoll)</a> · Open source under MIT</p>
    </div>
  </footer>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=os.getenv("GOOGLE_CLOUD_PROJECT", DEFAULT_PROJECT),
        help=f"Google Cloud project id (default: {DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--poll-id",
        default=None,
        help="Poll definition id in Firestore (defaults to id in config file)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to poll.yaml (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to save JSON and HTML results (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help="Base name for output files without extension (defaults to aaif-luxembourg-1-vibepoll-results or poll id)",
    )
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        help="Build HTML report from an existing JSON results file instead of querying Firestore",
    )

    args = parser.parse_args()

    config = load_config(args.config)
    poll_id = args.poll_id or config.id

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.from_json:
        print(f"Loading results from {args.from_json}...", file=sys.stderr)  # noqa: T201
        data = json.loads(args.from_json.read_text(encoding="utf-8"))
        if poll_id.startswith("aaif-luxembourg-1") or data.get("event", {}).get("id", "").startswith(
            "aaif-luxembourg-1"
        ):
            data["event"] = AAIF_LUXEMBOURG_METADATA.copy()
    else:
        print(f"Fetching results from Firestore (project={args.project}, poll={poll_id})...", file=sys.stderr)  # noqa: T201
        data = asyncio.run(fetch_results(args.project, poll_id, config))

    # Base filename
    if args.output_name:
        base_name = args.output_name
    elif poll_id.startswith("aaif-luxembourg-1"):
        base_name = "aaif-luxembourg-1-vibepoll-results"
    else:
        base_name = poll_id

    # File names
    json_path = output_dir / f"{base_name}.json"
    html_path = output_dir / f"{base_name}.html"

    # Save JSON
    json_content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    json_path.write_text(json_content, encoding="utf-8")
    print(f"✓ Saved JSON: {json_path} ({len(json_content)} bytes)", file=sys.stderr)  # noqa: T201

    # Save HTML
    html_content = render_html_report(data)
    html_path.write_text(html_content, encoding="utf-8")
    print(f"✓ Saved HTML: {html_path} ({len(html_content)} bytes)", file=sys.stderr)  # noqa: T201

    print(f"\nSuccessfully exported results for {data['event']['title']}!", file=sys.stderr)  # noqa: T201
    print(f"  Participants : {data['poll']['participants']}", file=sys.stderr)  # noqa: T201
    print(f"  Total ballots: {data['poll']['total_ballots']}", file=sys.stderr)  # noqa: T201
    print(f"  HTML report  : {html_path.resolve()}", file=sys.stderr)  # noqa: T201
    print(f"  JSON data    : {json_path.resolve()}", file=sys.stderr)  # noqa: T201

    return 0


if __name__ == "__main__":
    sys.exit(main())
