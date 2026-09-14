# vibepoll

Live audience poll for a room with a projector.

Attendees scan a QR code, vote anonymously on their phones, and watch live results update on the big screen. No sign-ups or accounts required. Questions, pacing, and branding are configured in a single YAML file.

## Quickstart

```sh
mise install
mise run install
mise run watch
```

- **Audience view**: Open `http://localhost:8080`.
- **Presenter console**: Open `/present?key=...` with the presenter key printed in the startup logs.

Without `GOOGLE_CLOUD_PROJECT`, votes stay in memory.

## How it works

Three surfaces share one live state:

| Surface      | URL                | Audience                               |
| ------------ | ------------------ | -------------------------------------- |
| Voting       | `/`                | Audience (phones)                      |
| Stage screen | `/present?key=...` | Projector display & presenter controls |
| QR code      | `/qr.svg`          | Joins voting page                      |

The event runs in three phases:

1. **`slideshow` (break)**: The screen splits between a persistent QR code on the right and an animated carousel of live question tallies on the left. Attendees vote at their own pace.
1. **`review` (on stage)**: Voting closes, the QR code disappears, and questions expand to full width for presenter commentary. Audience phones switch to a "Look up" screen.
1. **`finished`**: Displays a closing thank-you screen.

### Presenter controls

Available at `/present` through on-screen buttons or keyboard shortcuts:

| Key       | Break (`slideshow`)            | Review (`review`)              |
| --------- | ------------------------------ | ------------------------------ |
| `Space`   | Pause / resume carousel        | Next question                  |
| `←` / `→` | Step slides manually           | Previous / next question       |
| `s`       | Return to carousel             | Return to carousel             |
| `r`       | Close voting & enter review    | —                              |
| `f`       | Show thank-you screen          | Show thank-you screen          |
| Wipe      | Reset all ballots (asks first) | Reset all ballots (asks first) |

## Poll configuration

Define the deck and pacing in `poll.yaml`:

```yaml
id: my-meetup-1
title: "Agentic AI Night #1"
subtitle: "AAIF Luxembourg Launch"
repo: https://github.com/fmind/vibepoll
panel_seconds: 8

questions:
  - id: experience
    title: "Experience with AI agents?"
    options:
      - id: beginner
        label: "Beginner"
      - id: builder
        label: "Builder"

  - id: message
    type: text
    title: "What would you like to share tonight?"
    subtitle: "Your message will appear on the big screen. Up to 500 characters."
```

- Choice questions (`type: choice`, default) require 2–10 `options`.
- Free-text questions (`type: text`) accept messages up to 500 characters.
- Question and option `id`s act as stable storage keys; reword labels freely without losing ballots.
- Use new question IDs when replacing questions, or a new poll `id` to start a separate event. Results and submitted messages are public.

### Environment variables

| Variable               | Default                    | Purpose                                                        |
| ---------------------- | -------------------------- | -------------------------------------------------------------- |
| `VIBEPOLL_CONFIG`      | `poll.yaml`                | Path to poll configuration.                                    |
| `PUBLIC_URL`           | `http://localhost:8080`    | URL encoded into the QR code (marks cookie `Secure` on HTTPS). |
| `PRESENTER_KEY`        | random (logged at startup) | Secret key granting access to `/present`.                      |
| `GOOGLE_CLOUD_PROJECT` | unset (in-memory)          | Google Cloud project ID for Firestore persistence.             |
| `PORT`                 | `8080`                     | Server listening port.                                         |

## Deployment

vibepoll runs as a single container on Cloud Run backed by Firestore in Native mode:

```sh
PROJECT="your-project-id"
REGION="europe-west1"

# 1. Enable services
gcloud services enable run.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project="$PROJECT"

# 2. Create Firestore database (eur3 is multi-region Europe)
gcloud firestore databases create --location=eur3 --project="$PROJECT"

# 3. Create service account with Firestore access
gcloud iam service-accounts create vibepoll-run --project="$PROJECT"
SA="vibepoll-run@$PROJECT.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role=roles/datastore.user

# 4. Store presenter key
python3 -c "import secrets; print(secrets.token_urlsafe(8))" \
  | gcloud secrets create vibepoll-presenter-key --data-file=- --project="$PROJECT"
gcloud secrets add-iam-policy-binding vibepoll-presenter-key \
  --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor --project="$PROJECT"

# 5. Deploy
uv run python scripts/deploy.py --project "$PROJECT" --region "$REGION"
```

Deployment notes:

- `poll.yaml` is baked into the image: redeploy after changing it. GitHub Actions validates pushes but does not deploy them.
- Use `--min-instances=1` before an event to eliminate cold starts (`--min-instances=0` costs nothing idle).
- Pass `--public-url https://...` when mapping a custom domain.

## Architecture

- **Single instance**: Cloud Run is pinned to `--max-instances=1`. Room capacity scales through concurrency over long-lived SSE connections (`/events`).
- **State restoration**: On startup, tallies are reconstructed from Firestore ballots, and the presenter phase and position are restored from a separate state document.
- **Anonymous voters**: Ballots are keyed `{question_id}__{voter_id}` using a random browser token in `localStorage`.

### Project layout

- `poll.yaml`: Questions, options, pacing, and branding.
- `src/vibepoll/config.py`: Schema validation for the poll file.
- `src/vibepoll/store.py`: In-memory tally, run-of-show state machine, and SSE fan-out.
- `src/vibepoll/firestore.py`: Firestore ballot repository.
- `src/vibepoll/app.py`: Routes, presenter auth, and security headers.
- `src/vibepoll/settings.py`: Environment configuration.

## Development

```sh
mise run check  # Format, lint, types, workflows, and security scans
mise run test   # Pytest and Playwright browser tests
mise run all    # Complete validation gate
```

## License

[MIT](LICENSE)
