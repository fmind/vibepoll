#!/usr/bin/env python3
"""Deploy vibepoll to Cloud Run from source.

Run the one-time setup in README.md first. This script is the repeatable part:
it validates its inputs, then hands gcloud a fixed argument vector — never a
shell string — so a stray character in an argument cannot extend the command.

Usage:
    uv run python scripts/deploy.py --project <id> [--region europe-west1]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys

DEFAULT_SERVICE = "vibepoll"
DEFAULT_REGION = "europe-west1"
KEY_SECRET_NAME = "vibepoll-presenter-key"  # noqa: S105 - a Secret Manager resource name, not a credential
# Google's own identifier grammars. Rejecting early gives a clearer error than a
# gcloud stack trace and keeps an arbitrary string out of the argument vector.
PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REGION_PATTERN = re.compile(r"^[a-z]+-[a-z]+[0-9]$")
SERVICE_PATTERN = re.compile(r"^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$")


def run(argv: list[str]) -> str:
    """Run a command, echoing it first, and return its stdout.

    Raises:
        SystemExit: the command failed. gcloud puts its diagnosis on stderr, so
            that output is surfaced verbatim rather than swallowed by the
            capture — a bare exit status says nothing about how to recover.
    """
    print("+", " ".join(argv), file=sys.stderr)  # noqa: T201 - a deploy script narrates
    result = subprocess.run(argv, capture_output=True, text=True)  # noqa: S603 - fixed argv, checked below
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)  # noqa: T201
        print(result.stderr, file=sys.stderr)  # noqa: T201
        raise SystemExit(f"command failed with exit status {result.returncode}: {argv[0]} {argv[1]} {argv[2]}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Google Cloud project id")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"Cloud Run region (default: {DEFAULT_REGION})")
    parser.add_argument(
        "--service", default=DEFAULT_SERVICE, help=f"Cloud Run service name (default: {DEFAULT_SERVICE})"
    )
    parser.add_argument("--service-account", default=None, help="runtime service account email")
    parser.add_argument(
        "--public-url",
        default=None,
        help="origin the QR code encodes; defaults to the assigned run.app URL. "
        "Point it at a custom domain only once that domain actually serves TLS",
    )
    parser.add_argument(
        "--min-instances",
        type=int,
        default=0,
        choices=(0, 1),
        help="0 scales to zero between sessions (default); 1 keeps an instance warm and billable",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=800,
        help="simultaneous requests per instance. Every phone holds one SSE connection open for the whole "
        "session, so this is effectively the room size plus headroom for votes and assets (default: 800)",
    )
    args = parser.parse_args()

    if not PROJECT_PATTERN.match(args.project):
        parser.error(f"invalid project id: {args.project!r}")
    if not REGION_PATTERN.match(args.region):
        parser.error(f"invalid region: {args.region!r}")
    if not SERVICE_PATTERN.match(args.service):
        parser.error(f"invalid service name: {args.service!r}")
    if not 1 <= args.concurrency <= 1000:
        parser.error(f"concurrency must be between 1 and 1000, got {args.concurrency}")
    if shutil.which("gcloud") is None:
        parser.error("gcloud is not on PATH")

    service_account = args.service_account or f"{args.service}-run@{args.project}.iam.gserviceaccount.com"

    base = [
        "gcloud",
        "run",
        "deploy",
        args.service,
        "--source=.",
        f"--project={args.project}",
        f"--region={args.region}",
        f"--service-account={service_account}",
        "--allow-unauthenticated",
        # Exactly one instance, always. The live tally lives in this process, so
        # a second instance would serve a second, disagreeing poll: two rooms
        # voting into two ballot boxes with one projector. Room capacity is
        # raised with --concurrency, never by lifting this.
        "--max-instances=1",
        # min-instances=0 costs nothing between sessions and loses nothing:
        # state and every ballot are rebuilt from Firestore on the next request.
        # The only cost is a cold start for whoever scans first. Pass
        # --min-instances=1 shortly before a session to pay for a warm instance.
        f"--min-instances={args.min_instances}",
        f"--concurrency={args.concurrency}",
        "--cpu=1",
        "--memory=512Mi",
        # Every phone holds one SSE connection open for the whole session; the
        # 5-minute default would cut the room off every five minutes.
        "--timeout=3600",
        "--cpu-boost",
        f"--update-secrets=PRESENTER_KEY={KEY_SECRET_NAME}:latest",
    ]

    def deploy(public_url: str | None) -> None:
        """Create or update the service. ``public_url`` is what the QR encodes."""
        # The poll id, titles and questions all live in poll.yaml, baked into the
        # image, so the environment carries only deployment facts.
        environment = {"GOOGLE_CLOUD_PROJECT": args.project}
        if public_url is not None:
            environment["PUBLIC_URL"] = public_url
        settings = ",".join(f"{name}={value}" for name, value in environment.items())
        run([*base, f"--set-env-vars={settings}"])

    describe = [
        "gcloud",
        "run",
        "services",
        "describe",
        args.service,
        f"--project={args.project}",
        f"--region={args.region}",
        "--format=value(status.url)",
    ]

    if args.public_url:
        # The origin is already known, so one revision is enough.
        deploy(public_url=args.public_url)
        url = args.public_url
        run(describe)
    else:
        # PUBLIC_URL is what the QR encodes and what marks the presenter cookie
        # Secure, but Cloud Run only reveals the service URL after the first
        # deploy. A second revision carrying the real origin is simpler than
        # reserving a domain up front; on a later run it is already correct.
        deploy(public_url=None)
        url = run(describe)
        deploy(public_url=url)

    key = run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={KEY_SECRET_NAME}",
            f"--project={args.project}",
        ]
    )
    print(f"\n  Audience  : {url}")  # noqa: T201
    print(f"  Presenter : {url}/present?key={key}\n")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
