"""Runtime configuration parsed once at the process boundary.

The split is deliberate: the environment describes the *deployment* (where the
process runs, what secret guards the console, where the durable store lives),
and the YAML poll definition describes the *event*. A misconfigured process
fails at startup with a message naming the variable rather than at the first
request during a talk.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Settings", "load_settings"]

_DEFAULT_CONFIG = "poll.yaml"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable deployment configuration."""

    config_path: Path
    presenter_key: str
    project_id: str | None
    public_url: str
    generated_key: bool

    @property
    def is_https(self) -> bool:
        """Whether the public origin is HTTPS.

        Cloud Run terminates TLS at the proxy, so the application sees ``http``
        on every request. Cookie and HSTS decisions must come from the declared
        public origin, not from the request the process happens to observe.
        """
        return self.public_url.startswith("https://")

    @property
    def use_firestore(self) -> bool:
        """Persist votes only when a Google Cloud project is configured."""
        return self.project_id is not None


def _required_text(name: str, default: str) -> str:
    raw = os.environ.get(name, default).strip()
    if not raw:
        msg = f"{name} must not be blank"
        raise ValueError(msg)
    return raw


def load_settings() -> Settings:
    """Build settings from the environment, failing fast on invalid values."""
    presenter_key = os.environ.get("PRESENTER_KEY", "").strip()
    generated_key = not presenter_key
    if generated_key:
        # Never fall back to an empty or guessable key: an unset variable gets a
        # fresh random one that is logged once, so the presenter routes are
        # always protected even on a hurried local run.
        presenter_key = secrets.token_urlsafe(8)
    elif len(presenter_key) < 8:
        msg = "PRESENTER_KEY must be at least 8 characters"
        raise ValueError(msg)

    return Settings(
        config_path=Path(_required_text("VIBEPOLL_CONFIG", _DEFAULT_CONFIG)),
        presenter_key=presenter_key,
        project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip() or None,
        public_url=_required_text("PUBLIC_URL", "http://localhost:8080").rstrip("/"),
        generated_key=generated_key,
    )
