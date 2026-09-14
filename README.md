# vibepoll

Live audience poll for a room with a projector.

People scan a QR code, answer on their phone during the break, and the results build on the big screen while they do. Anonymous, no sign-up, no accounts. The questions, the pacing, and the branding are one YAML file.

Slido, except vibe-coded.

Built for [Agentic AI Night #1](https://luma.com/4ciica7u), the AAIF Luxembourg launch. The default `poll.yaml` contains six questions about agent experience, attendee roles, use cases, reliability, barriers, and the next session — replace that one file and it is your poll.

## How it works

Three surfaces, one shared state:

| Surface      | URL              | Who                                              |
| ------------ | ---------------- | ------------------------------------------------ |
| Vote         | `/`              | the room, on their phones                        |
| Stage screen | `/present?key=…` | you, once — then the key is swapped for a cookie |
| QR code      | `/qr.svg`        | rendered into the stage screen                   |

The session runs in three phases.

**`slideshow` — during the break.** Every question is open at once and the room answers at its own pace: scan, answer question 1, the phone advances to question 2 after the save is confirmed, and so on to the end. A failed request stays on the question with a retry message. Native radio controls support keyboard navigation, and a numbered progress label tracks the remaining questions.

The stage screen splits: the QR holds the right third for the entire break, and only the left two thirds animate, cycling the welcome panel and then each question with its live results. The code never rotates away, so nobody loses it mid-scan and a latecomer can always join. `panel_seconds` in the poll file sets the dwell time.

**`review` — when you go back on stage.** Voting closes, the rotation stops, and the question takes the full width with the QR gone, under your control so you can comment on each result. Every phone switches to "Look up" and shows nothing else, which is the point: attention belongs on the screen, not in 60 hands.

**`finished`.** A thank-you screen closes the session. Results remain available by returning to Review.

The rotation is timed **in the browser**, not on the server. A server-driven one would have to push a frame to every phone in the room every few seconds just to move a panel nobody on a phone can see, and two screens would still drift apart after a reconnect. The timer is armed once per panel rather than restarted on every render, or a room voting faster than `panel_seconds` would freeze the carousel on one slide for the whole break.

### Presenter controls

Use the buttons at the bottom of the stage screen, or use the keyboard:

| Key       | During the break                                          | While reviewing            |
| --------- | --------------------------------------------------------- | -------------------------- |
| `space`   | Pause / resume the carousel                               | Next question              |
| `←` / `→` | Step slides by hand (pauses)                              | Previous / next question   |
| `s`       | Back to the break carousel                                | Back to the break carousel |
| `r`       | Take the stage (closes voting)                            | —                          |
| `f`       | Thank-you screen                                          | Thank-you screen           |
| Wipe      | Deletes every vote. Asks first. Button only, no shortcut. |                            |

A strip along the bottom of the stage screen carries the repository URL and, when the stream drops, a `reconnecting…` marker — so a frozen screen never looks like a live one.

## The poll file

Everything about the event lives in `poll.yaml`. The environment describes the deployment; this file describes the poll.

```yaml
id: my-meetup-1 # Firestore document key. Change it to start a fresh poll.
title: "Agentic AI Night #1"
subtitle: "AAIF Luxembourg Launch" # optional, shown above the title
repo: https://github.com/fmind/vibepoll # optional, shown on the stage screen
panel_seconds: 8 # optional, dwell per panel during the break (default 5)

questions:
  - id: familiarity # stable key: reword the title freely, never the id
    title: "Which best describes your experience with AI agents?"
    subtitle: "Pick the closest match." # optional
    options:
      - id: unused
        label: "Never used agents"
      - id: tried
        label: "Tried agents occasionally"
      - id: user
        label: "Use agents regularly without building them"
      - id: builder
        label: "Build agents"
```

| Field                 | Required | Notes                                                            |
| --------------------- | -------- | ---------------------------------------------------------------- |
| `id`                  | yes      | `[A-Za-z0-9_-]`, 1–64 characters. Keys the Firestore document.   |
| `title`               | yes      | Shown on both screens and in the browser tab.                    |
| `subtitle`            | no       | Eyebrow line above the title.                                    |
| `repo`                | no       | `https://…`. Rendered on the stage screen's bottom strip.        |
| `panel_seconds`       | no       | 0 < n ≤ 300. Default `5`.                                        |
| `questions`           | yes      | 1–30, each with a unique `id`.                                   |
| `questions[].options` | yes      | 2–10 per question, each with a unique `id` within that question. |

The file is read and validated once at startup, so a typo stops the process with the field path rather than showing an empty screen to a room. Duplicate ids, a missing field, an unknown key, and an out-of-range `panel_seconds` are all refused.

Two YAML traps the schema will catch but that are easier to avoid: a bare `yes`, `no`, `on` or `off` parses as a **boolean**, so quote an option id spelled like one; and an unquoted `Night #1` loses everything from the `#` onward. The shipped `poll.yaml` quotes all prose for exactly this reason.

**Option ids are the storage key.** Rewording a label leaves existing ballots intact. If the answer meanings change, use a new poll `id` so earlier responses remain separate; the shipped six-question deck uses `aaif-luxembourg-1-v2`. Shortening the deck between sessions is safe: ballots for questions that no longer exist are dropped at startup rather than resurrected.

## Run it locally

```sh
mise install
mise run install
mise run watch
```

Then open <http://localhost:8080> for the phone view. The presenter key is printed to the log on startup when `PRESENTER_KEY` is unset — the console lives at `/present?key=…`.

Without `GOOGLE_CLOUD_PROJECT`, votes are kept in memory and lost on restart. That is the right default for local work.

### Environment

| Variable               | Default                 | Purpose                                                               |
| ---------------------- | ----------------------- | --------------------------------------------------------------------- |
| `VIBEPOLL_CONFIG`      | `poll.yaml`             | Path to the poll definition.                                          |
| `PUBLIC_URL`           | `http://localhost:8080` | What the QR code encodes. `https://…` also marks the cookie `Secure`. |
| `PRESENTER_KEY`        | random, logged once     | Unlocks `/present`. Minimum 8 characters.                             |
| `GOOGLE_CLOUD_PROJECT` | unset (memory-only)     | Enables Firestore persistence.                                        |
| `PORT`                 | `8080`                  | Set by Cloud Run.                                                     |

`PUBLIC_URL` is the origin of truth for HTTPS, not the request scheme: Cloud Run terminates TLS at its proxy, so the process only ever sees `http`.

## Architecture

One Cloud Run instance serves the whole room. Each vote is saved to Firestore before it enters the in-memory tally or is broadcast. Writes and presenter controls are serialized to prevent a delayed save from undoing a changed answer or surviving a wipe. Reads stay available during saves; storage latency limits write throughput. A restart rebuilds the tally from confirmed ballots.

This is why the service is pinned to `--max-instances=1`. A second instance would serve a second, disagreeing poll. Room capacity is raised with `--concurrency` — every phone holds one SSE connection open for the whole session — never by lifting the instance cap.

Ballots are keyed `{question_id}__{voter_id}`, where the voter id is a random value minted by the browser and kept in `localStorage`. Changing your answer moves your vote instead of adding one, and the application stores the answer, random browser ID, and timestamp, without collecting an account, address, or user agent. Results keep the configured answer order, including frequency scales, and use labels without answer letters. Because the server remembers which questions a voter has answered, a phone that reloads mid-deck resumes at its first unanswered question.

Results are public — the room watches them build on the stage screen through the whole break — so nothing is withheld from a phone. The presenter key guards the run of show (`/api/control`), not the numbers.

Streamed frames carry only ids and counts. Question prose is fetched once from `/api/poll` and joined on the id, which is what makes it affordable to push a fresh frame to every phone on every vote. The fan-out queue is one slot deep and carries no payload: it is a "something changed" flag, so a burst of votes collapses into a single re-render and a slow subscriber is never evicted.

### Layout

- `poll.yaml` — the questions, the pacing, the branding.
- `src/vibepoll/config.py` — the poll file's schema, validated at startup.
- `src/vibepoll/store.py` — live tally, run-of-show state machine, SSE fan-out.
- `src/vibepoll/firestore.py` — durable storage.
- `src/vibepoll/app.py` — composition root: routes, presenter check, security headers.
- `src/vibepoll/settings.py` — every environment variable is parsed here and nowhere else.
- `src/vibepoll/templates/`, `src/vibepoll/static/` — two pages, one stylesheet, no build step and no framework.

## Deploy

One-time setup, run with eyes on it:

```sh
PROJECT=your-project-id
REGION=europe-west1

gcloud services enable run.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project="$PROJECT"

# Firestore in Native mode. eur3 is multi-region Europe.
gcloud firestore databases create --location=eur3 --project="$PROJECT"

# A dedicated runtime identity that can write votes and read the presenter key.
gcloud iam service-accounts create vibepoll-run --project="$PROJECT"
SA="vibepoll-run@$PROJECT.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role=roles/datastore.user

# The presenter key, generated once and never committed.
python3 -c "import secrets; print(secrets.token_urlsafe(8))" \
  | gcloud secrets create vibepoll-presenter-key --data-file=- --project="$PROJECT"
gcloud secrets add-iam-policy-binding vibepoll-presenter-key \
  --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor --project="$PROJECT"
```

Then deploy, and re-deploy, with:

```sh
uv run python scripts/deploy.py --project "$PROJECT" --region "$REGION"
```

It prints the audience URL and the presenter URL. The first run deploys twice on purpose: Cloud Run only reveals the service URL after the service exists, and that URL is what the QR code has to encode.

`poll.yaml` is baked into the image, so editing the questions means redeploying — which is the point: the deck is reviewed in a diff.

### Cost

Well inside the free tier for one evening. The default `--min-instances=0` costs nothing between sessions and loses nothing, because state and every ballot are rebuilt from Firestore on the next request; the only cost is a cold start for whoever scans first. Pass `--min-instances=1` shortly before a session to pay for a warm instance, and drop back to `0` after — or delete the service:

```sh
gcloud run services delete vibepoll --project="$PROJECT" --region="$REGION"
```

A custom domain needs `PUBLIC_URL` pointed at it, because that is what the QR encodes:

```sh
uv run python scripts/deploy.py --project "$PROJECT" --public-url https://vibepoll.example
```

Do that only once the domain actually serves TLS. Cloud Run reports `CertificateProvisioned: True` well before its edge serves the certificate — up to a few hours after the mapping is created — and until then a QR pointing at the domain is a dead QR.

## On the night

1. Deploy (`--min-instances=1` if you want to skip the cold start), open the presenter URL on the laptop, mirror to the projector.
2. At the start of the break, leave it on the break screen. The QR stays put on the right while the left side cycles the welcome and every question with live results, unattended, for as long as the break lasts.
3. People scan, answer at their own pace, and get a "thank you" screen once all answers are confirmed. They can revisit answers and use Done to return.
4. When you go back on stage, press `r`. Voting closes and every phone says "Look up".
5. `→` through the questions to comment on each, then `f` for the thank-you screen.

If the venue Wi-Fi drops, phones reconnect on their own and show the current state — `EventSource` retries without help, confirmed votes are in Firestore, and a page that loads mid-outage keeps retrying instead of sitting blank. If the instance restarts, it reloads state and every ballot from Firestore, so the run of show survives it.

Wipe every vote between a rehearsal and the real session, or the rehearsal's answers will be included in the results.

## Gotchas

The health endpoint is `/health`, **not** `/healthz`. Google Frontend intercepts the exact path `/healthz` in front of Cloud Run and answers it with its own HTML 404, so the request never reaches the container. Verified against the deployed service; `/health`, `/livez` and `/readyz` all pass through normally.

There is no rate limit on `/api/vote`. Voter ids are minted client-side, so anyone who reads the page can script ballots. For a friendly room with public results that is the right trade; for anything where the numbers matter, it is not.

## Verify

```sh
mise run all         # format, lint, types, workflows, secret scan, dependency scan, tests
mise run test        # Python and Chromium regressions; 85% branch-coverage gate
mise run check:image # build and scan the production OCI image (needs Docker)
```

The install task downloads Chromium for browser tests. On a fresh Linux machine, install Playwright's required system libraries through your system administrator; CI installs them on its disposable runner.

## Licence

MIT.
