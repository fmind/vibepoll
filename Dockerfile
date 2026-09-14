# syntax=docker/dockerfile:1
# Deliberately free of BuildKit-only features (`RUN --mount=type=cache|bind`):
# `gcloud run deploy --source` builds with the classic Cloud Build Docker
# builder, which has BuildKit disabled and fails on them. Dependency layer
# caching is preserved the portable way, by copying the lock files before the
# source so a code-only change does not re-resolve the environment.
FROM python:3.14.7-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS base
# Security fixes not yet included in the pinned Python image (2026-09-13).
RUN apt-get update \
  && apt-get install --yes --no-install-recommends --only-upgrade \
    gzip=1.13-1+deb13u1 \
    libpcre2-8-0=10.46-1~deb13u2 \
    libsqlite3-0=3.46.1-7+deb13u2 \
    perl-base=5.40.1-6+deb13u1 \
  && rm -rf /var/lib/apt/lists/*

FROM base AS build
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev
COPY . /app
RUN uv sync --locked --no-dev --no-editable \
  && chmod -R u=rwX,go=rX /app/.venv

FROM base AS runner
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# The runtime installs only the locked application environment; retaining pip
# adds an unused package installer and its vendored dependency attack surface.
RUN python -m pip uninstall --yes --root-user-action=ignore pip \
  && groupadd --gid 10001 app \
  && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
  # This runtime never needs privileged Debian login or mount helpers.
  && find /usr -xdev -type f -perm /6000 -exec chmod a-s {} +
USER 10001:10001
EXPOSE 8080
# Templates and static assets ship inside the package, so the virtualenv is the
# entire application. It stays root-owned: the runtime identity reads it and has
# no reason to modify its own code.
COPY --from=build /app/.venv /app/.venv
# The poll definition is content, not code, so it sits beside the virtualenv
# rather than inside the package. WORKDIR is /app, which is where the default
# VIBEPOLL_CONFIG resolves to; mount or rebuild to change the questions.
COPY poll.yaml /app/poll.yaml
ENTRYPOINT ["vibepoll"]
