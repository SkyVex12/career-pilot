# Copilot

Desktop career copilot: listens to live interviews and meetings, detects questions aimed at
the user, and shows answers grounded in their career memory. The design is in
[ARCHITECTURE.md](ARCHITECTURE.md), which is the source of truth for everything here.

Status: Stage 0, build step 2. The gateway serves live sessions (phase 1, "Live answers",
gateway side): the WebSocket protocol, Deepgram STT on the "them" channel, turn detection,
the Haiku detector, fast and deep answers from the hot digest and session pack, session state
and logs in Valkey, and jobs on the queue. The API and workers still answer health checks only.

## Layout

```
apps/
  desktop/      Tauri 2 app: Rust core + React UI           (pnpm)
  gateway/      FastAPI WebSocket service, port 8081         (uv)
  api/          FastAPI REST service, port 8080              (uv)
  worker/       SQS consumer; health check on port 8082      (uv)
packages/
  core/         shared Python; copilot-migrate lives here
  protocol/     protocol.schema.json + generated Python and TypeScript types
migrations/     plain SQL migrations and the row-level security test
evals/          copilot-replay: stream a recorded call through the gateway
infra/          Terraform (empty)
scripts/        codegen.py, dev_profile.py, record_fixtures.py
```

## Prerequisites

The commands below work the same in PowerShell, Git Bash, macOS and Linux.

| Tool | Version | Needed for | Install (Windows) |
| --- | --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | 0.8+ | Python; it fetches Python 3.12 itself | `winget install astral-sh.uv` |
| Node.js | 22+ | TypeScript, codegen | `winget install OpenJS.NodeJS.LTS` |
| pnpm | 10 | TypeScript workspace | `npm install -g pnpm@10` |
| Docker Desktop | current | Local stack (needs WSL 2) | `winget install Docker.DockerDesktop` |
| Rust (rustup) | stable | Only to run the desktop app in Tauri | [tauri.app prerequisites](https://v2.tauri.app/start/prerequisites/) |

## Setup

```sh
uv sync          # Python workspace into .venv
pnpm install     # TypeScript workspace
```

## Local stack

`docker-compose.yml` runs Postgres 16 with pgvector, Valkey and ElasticMQ (an SQS-compatible
queue with the `jobs` queue and its dead-letter queue). Ports bind to localhost only.

```sh
docker compose up -d --wait   # start; returns when Postgres and Valkey are healthy
uv run copilot-migrate        # apply migrations/ to the copilot database
docker compose down           # stop (add -v to delete the data too)
```

| Service | Address | Credentials |
| --- | --- | --- |
| Postgres | `postgresql://postgres:postgres@localhost:5432/copilot` | superuser, local only |
| Valkey | `redis://localhost:6379` | none |
| ElasticMQ | `http://localhost:9324`, UI on `http://localhost:9325` | any key, region `us-east-1` |

`.env.example` lists the same defaults. If another Postgres already uses port 5432, stop it
or change the port mapping in `docker-compose.yml` and set `COPILOT_DATABASE_URL`.

### Running the services

Each command runs in the foreground; use one terminal per service.

```sh
uv run copilot-gateway        # http://127.0.0.1:8081/healthz
uv run copilot-api            # http://127.0.0.1:8080/healthz
uv run copilot-worker         # http://127.0.0.1:8082/healthz
pnpm --filter @copilot/desktop dev     # desktop UI in a browser, http://localhost:1420
pnpm --filter @copilot/desktop tauri dev   # desktop app in Tauri (needs Rust)
```

`HOST` and `PORT` override the bind address. Check a service with
`curl http://127.0.0.1:8081/healthz`.

### Running the gateway locally

The gateway needs a `.env` (gitignored; start from `.env.example`) with:

| Variable | For |
| --- | --- |
| `COPILOT_ENV=dev`, `COPILOT_AUTH=dev`, `COPILOT_DEV_TOKEN` | Dev auth: a static bearer token for `COPILOT_DEV_USER_ID`. It works only with `COPILOT_ENV=dev` and never inside AWS. Cognito comes later |
| `COPILOT_LLM_PROVIDERS` | `anthropic`, `bedrock`, or both in fallback order |
| `ANTHROPIC_API_KEY` | The direct Claude API |
| AWS credentials or `AWS_BEARER_TOKEN_BEDROCK` | Claude on Bedrock (Mantle), and Cohere embeddings when `COPILOT_EMBEDDER=bedrock` |
| `DEEPGRAM_API_KEY` | Streaming STT. Without it the gateway runs, but only typed questions (`question.force` with text) get answers |

Every live setting from ARCHITECTURE.md ("Settings") is an environment variable with the
prefix `COPILOT_`, for example `COPILOT_SILENCE_TIMER_MS=400`. See
`apps/gateway/src/copilot_gateway/config.py` for the full list.

Load a digest and an interview session pack for the dev user, built by Claude from a resume
and a job description (PDF, Markdown or text):

```sh
uv run python scripts/dev_profile.py --resume resume.pdf --job job.md --round technical
```

Then run the gateway and replay a recorded call through it in real time:

```sh
uv run copilot-gateway
uv run copilot-replay clip.wav            # prints questions, answers, latency per stage
uv run copilot-replay clip.wav --json run.json --drop-at 30
```

`copilot-replay` plays the WAV as the "them" channel with Silero VAD on the client side, the
way the desktop app does. It reports first-word latency p50/p95 against the 0.9 s / 1.5 s
target, measured from the true end of speech, so the VAD hangover counts too.

### Database roles

Migration 0001 creates the roles from ARCHITECTURE.md (`app_owner`, `app_api`,
`app_gateway`, `app_worker`, `app_system`) as `NOLOGIN`. Migrations hold no passwords. Each
environment grants login separately: Terraform in AWS, and later a local dev script when the
services start connecting. Every request must run
`select set_config('app.user_id', $1, true)` in its transaction. Without it, queries on user
tables raise an error rather than return rows.

## Protocol codegen

`packages/protocol/protocol.schema.json` is the realtime protocol contract. After editing it,
regenerate both languages and commit the result:

```sh
pnpm codegen          # writes python/copilot_protocol/models.py and src/generated.ts
pnpm codegen:check    # exits 1 and prints a diff if the generated files are stale
```

Generated files are never edited by hand. Python uses datamodel-code-generator and
TypeScript uses json-schema-to-typescript; both versions are pinned by the lockfiles.

## Tests and checks

```sh
uv run pytest                 # all Python tests, including those that need the stack
uv run pytest -m "not stack"  # without the local stack
uv run pytest -m stack        # only the tests against the local stack
pnpm test                     # TypeScript contract tests (vitest)
pnpm typecheck                # tsc for protocol and desktop
pnpm codegen:check            # generated protocol types are current
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

- **Contract tests** (`packages/protocol/tests/`): the fixtures in
  `packages/protocol/fixtures/messages.json` round-trip through the Python and TypeScript
  types. They cover every message type, messages that must be rejected, and audio frame
  headers that must encode to the same bytes in both languages.
- **Row-level security test** (`migrations/tests/test_rls.py`, marker `stack`): migrates a
  throwaway database, seeds two users across every user table, and checks for `app_api`,
  `app_worker` and `app_gateway` that neither user can read or write the other's rows. It
  also checks that no row can reference another user's row: foreign keys between user
  tables are composite on `(id, user_id)`, and inserting or repointing a cross-user
  reference must fail for every relationship. It fails if a new table with `user_id` lacks
  forced row-level security, or a new foreign key between user tables lacks `user_id`. It
  needs the Postgres from `docker compose`, or any Postgres 16+ with pgvector, given as a
  superuser URL in `COPILOT_DATABASE_URL`.
- **Local stack smoke tests** (`tests/test_local_stack.py`, marker `stack`): a set and get
  with TTL on Valkey; on ElasticMQ, the `jobs` queue's dead-letter policy and a create,
  send, receive and delete with a `MessageGroupId`.
- **Gateway tests** (`apps/gateway/tests/`): each test runs a real uvicorn server with
  fakeredis, a scripted STT that reads words from the audio bytes, and scripted Claude routes.
  They cover the protocol flow, speculative start and cancel, a new question cancelling the
  previous answer, resume on the same and on another task without duplicate frames, one live
  session per user (`4409`), auth, limits and close codes, and each failure-mode row the
  gateway owns: model fallback, STT reconnect and provider switch, a task dying mid-call, and
  Valkey unavailable. `test_stack.py` (marker `stack`) repeats the Valkey and queue parts
  against the compose stack.
- **Provider fixtures** (`apps/gateway/tests/fixtures/`): the Anthropic SDK stream and the
  Deepgram WebSocket are replayed from wire-format recordings, so no test needs the network.
  `uv run python scripts/record_fixtures.py --wav clip.wav` re-records them with real keys.
- **Replay tool tests** (`evals/tests/`): `copilot-replay` against an in-process gateway,
  with and without a dropped socket.
