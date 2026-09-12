"""Process entry point.

The whole room is served by a single instance holding the live tally in memory,
so the server runs exactly one worker. Scaling out would split the poll into
independent tallies that disagree on the projector.
"""

from __future__ import annotations

import os

from granian import Granian
from granian.constants import Interfaces

__all__ = ["main"]


def main() -> None:
    """Serve the application on the platform-assigned port."""
    Granian(
        target="vibepoll.app:create_app",
        factory=True,
        address="0.0.0.0",  # noqa: S104 - container networking requires a wildcard bind
        port=int(os.environ.get("PORT", "8080")),
        interface=Interfaces.ASGI,
        workers=1,
        log_access=False,
    ).serve()


if __name__ == "__main__":
    main()
