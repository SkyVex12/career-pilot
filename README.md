# Copilot

Desktop career copilot: listens to live interviews and meetings, detects questions aimed at
the user, and shows answers grounded in their career memory. The design is in
[ARCHITECTURE.md](ARCHITECTURE.md), which is the source of truth for everything here.

Status: Stage 0, build step 4a. The gateway serves live sessions: the WebSocket protocol,
Deepgram STT on the "them" channel, turn detection, the Haiku detector, fast and deep answers
from the hot digest and session pack, session state and logs in Valkey, and jobs on the queue.
The Windows desktop app captures the call (WASAPI loopback) and the microphone, cancels echo,
runs Silero VAD, streams to the gateway and shows answers in an always-on-top overlay. The
Copilot API takes uploads (resume, job description), jobs, interviews and the user's audio,
and serves sessions with their post-call outputs. The workers turn a resume into the hot digest
and an interview into its session pack, copy the session log into Aurora with per-user
encryption, transcribe the user's audio, and write the summary and feedback. Login is a
Cognito access token, or the dev token locally; Cognito itself comes with Terraform (step 5).

## Layout

```
apps/
  desktop/      Tauri 2 app (Windows for now)               (pnpm, cargo)
    core/       copilot-client: capture, AEC, VAD, framing, gateway client
    src-tauri/  Tauri shell: windows, commands, hotkey, settings
    src/        React UI: overlay, start, settings, debug
  gateway/      FastAPI WebSocket service, port 8081         (uv)
  api/          FastAPI REST service, port 8080              (uv)
  worker/       SQS consumer, health check on port 8082;     (uv)
                copilot-scheduler (timed jobs)
packages/
  core/         shared Python: auth, crypto, db, S3, Claude routing;
                copilot-migrate and copilot-dev-roles live here
  protocol/     protocol.schema.json + generated Python, TypeScript and Rust types
migrations/     plain SQL migrations and the row-level security test
evals/          copilot-replay: stream a recorded call through the gateway
infra/          Terraform (empty)
scripts/        codegen.py, dev_profile.py, record_fixtures.py, record_worker_fixtures.py
```

## Prerequisites

The commands below work the same in PowerShell, Git Bash, macOS and Linux.

| Tool | Version | Needed for | Install (Windows) |
| --- | --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | 0.8+ | Python; it fetches Python 3.12 itself | `winget install astral-sh.uv` |
| Node.js | 22+ | TypeScript, codegen | `winget install OpenJS.NodeJS.LTS` |
| pnpm | 10 | TypeScript workspace | `npm install -g pnpm@10` |
| Docker Desktop | current | Local stack (needs WSL 2) | `winget install Docker.DockerDesktop` |
| Rust (rustup) | stable, 1.91+ | The desktop app and its tests | `winget install Rustlang.Rustup`, plus the MSVC build tools from the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) |

The first Rust build downloads a static ONNX Runtime (for Silero VAD) through the `ort` crate,
so it needs the network once. WebView2 ships with Windows 11.

## Setup

```sh
uv sync          # Python workspace into .venv
pnpm install     # TypeScript workspace
```

## Local stack

`docker-compose.yml` runs Postgres 16 with pgvector, Valkey, ElasticMQ (an SQS-compatible
queue with the `jobs` queue and its dead-letter queue) and an S3-compatible store (SeaweedFS;
MinIO no longer publishes images). Ports bind to localhost only.

```sh
docker compose up -d --wait   # start; returns when every service is healthy
uv run copilot-migrate        # apply migrations/ to the copilot database
uv run copilot-dev-roles      # local passwords and LOGIN for the five database roles
docker compose down           # stop (add -v to delete the data too)
```

| Service | Address | Credentials |
| --- | --- | --- |
| Postgres | `postgresql://postgres:postgres@localhost:5432/copilot` | superuser, local only |
| Valkey | `redis://localhost:6379` | none |
| ElasticMQ | `http://localhost:9324`, UI on `http://localhost:9325` | any key, region `us-east-1` |
| S3 (SeaweedFS) | `http://localhost:8333`, bucket `copilot-uploads` (created by the API and worker on start) | `local` / `local-secret` |

`.env.example` lists the same defaults. If another Postgres already uses port 5432, stop it
or change the port mapping in `docker-compose.yml` and set `COPILOT_DATABASE_URL`.

### Running the services

Each command runs in the foreground; use one terminal per service.

```sh
uv run copilot-gateway        # http://127.0.0.1:8081/healthz
uv run copilot-api            # http://127.0.0.1:8080/healthz, REST under /v1
uv run copilot-worker         # http://127.0.0.1:8082/healthz
uv run copilot-scheduler      # enqueues the timed jobs every minute (--once for one tick)
pnpm --filter @copilot/desktop tauri dev   # the desktop app (needs Rust; see below)
```

`HOST` and `PORT` override the bind address. Check a service with
`curl http://127.0.0.1:8081/healthz`.

### Running the API and workers locally

Both read the same `.env` as the gateway (start from `.env.example`). Besides the dev auth
variables below they need:

| Variable | For |
| --- | --- |
| `COPILOT_API_DATABASE_URL`, `COPILOT_WORKER_DATABASE_URL`, `COPILOT_SYSTEM_DATABASE_URL` | Each service's own role (`app_api`, `app_worker`, `app_system`); the defaults match `copilot-dev-roles`. `COPILOT_DATABASE_URL` is only for migrations |
| `COPILOT_KEY_PROVIDER=dev`, `COPILOT_DEV_MASTER_KEY` | Per-user data keys wrapped by a local master key instead of KMS (dev only; see ARCHITECTURE.md "Security and privacy"). Make one with `uv run python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` |
| `COPILOT_S3_ENDPOINT`, `COPILOT_S3_ACCESS_KEY`, `COPILOT_S3_SECRET_KEY` | The compose S3 store |
| `ANTHROPIC_API_KEY` or Bedrock credentials | Workers: resume extraction (Opus), job descriptions, packs and post-call outputs (Sonnet). On Bedrock they try the Flex tier first, then Standard |
| `DEEPGRAM_API_KEY` | Workers: transcribing the user's own audio after a call. Without it the audio is deleted untranscribed |

`COPILOT_FIXTURES_DIR=apps/worker/tests/fixtures` (dev and test only) makes the worker answer
every Claude and Deepgram call from the recorded fixtures, for running the whole flow without
keys. In AWS the API and workers use `COPILOT_AUTH=cognito` with
`COPILOT_COGNITO_USER_POOL_ID` and `COPILOT_COGNITO_CLIENT_IDS`, and `COPILOT_KEY_PROVIDER=kms`
with `COPILOT_KMS_KEY_ID` (build step 5).

The API (base path `/v1`, bearer token on every request):

| Endpoint | Does |
| --- | --- |
| `GET /me`, `PATCH /me`, `PATCH /me/settings` | Profile and settings. `PATCH /me` takes profile fields (display name), `PATCH /me/settings` the settings (retention days, standup time and timezone, consent defaults); each refuses the other's fields. The first request creates the user from the token |
| `DELETE /me` | Enqueues `user.delete`: rows, S3 files, Valkey keys and data keys |
| `POST /documents`, `GET /documents` | A presigned S3 upload (declare kind, content type, size, SHA-256) |
| `POST /documents/{id}/ingest` | A resume becomes career facts and the hot digest; a job description becomes a job and its session pack |
| `POST /jobs`, `GET /jobs` | Save a job (builds its pack); list jobs with their packs |
| `POST /interviews`, `GET /interviews` | Schedule an interview (builds its pack for the round) |
| `GET /sessions`, `GET /sessions/{id}` | Sessions; one session's status (`live`, `processing`, `ready`) and outputs |
| `GET /sessions/{id}/transcript`, `DELETE /sessions/{id}` | The decrypted transcript, if stored; delete a session and its log |
| `POST /sessions/{id}/me-audio`, `.../me-audio/complete` | Upload the user's own audio after the call; completing starts transcription |
| `GET /usage` | Live minutes this month (`?month=YYYY-MM` for another) |

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

Load a resume and a job description (PDF, Markdown or text) for the dev user through the API,
schedule an interview, and wait for the workers to build its session pack. The API and a
worker must be running:

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

### Running the whole thing on Windows

One terminal per long-running step.

1. **Stack:** `docker compose up -d --wait`, then `uv run copilot-migrate` and
   `uv run copilot-dev-roles` (once).
2. **`.env`** (from `.env.example`): `COPILOT_ENV=dev`, `COPILOT_AUTH=dev`, a
   `COPILOT_DEV_TOKEN` of 16+ characters, `COPILOT_DEV_MASTER_KEY`, Claude credentials
   (`ANTHROPIC_API_KEY` with `COPILOT_LLM_PROVIDERS=anthropic`, or Bedrock) and
   `DEEPGRAM_API_KEY`.
3. **API and worker:** `uv run copilot-api` and `uv run copilot-worker`, each in its own
   terminal (and `uv run copilot-scheduler` for the timed jobs, optional locally).
4. **Dev profile:** `uv run python scripts/dev_profile.py --resume resume.pdf --job job.md
   --round technical`. It uploads both through the API, schedules an interview an hour out,
   waits for the pack and prints the job id the start screen offers. A pack lives until a day
   after its interview.
5. **Gateway:** `uv run copilot-gateway` (`ws://127.0.0.1:8081/v1/session`). Bind
   `HOST=0.0.0.0` only if the app runs on another machine.
6. **App:** `pnpm --filter @copilot/desktop tauri dev`. The first build takes a few minutes.
7. **Settings** (in the app): gateway URL, the dev token (stored in the Windows Credential
   Manager), the microphone, and "Call audio from": the speakers or headset the call app plays
   on. Leave echo cancellation on. Save.
8. **Session:** pick the mode and job, the call app and the transcript consent, then Start
   session. The overlay opens on top; drag it by its header. The hotkey (Ctrl+Shift+Space by
   default) answers the last thing said when detection missed it; the box at the bottom of the
   overlay takes a typed question. Ctrl+Shift+D in the main window shows the debug panel:
   partial transcripts, detected questions, and latency per question (end of speech to first
   answer word, on the client, next to the gateway's `latency_ms`).

To check the devices without the app, `cargo run -p copilot-client --example capture_check -- 5`
lists them and reports how much audio each stream delivered (play something meanwhile;
loopback is silent otherwise).

To work on the UI without provider keys, run the gateway with scripted STT and Claude instead
of step 5: `uv run python -m copilot_gateway.offline --port 8081 --token <dev token>`. Typed
questions get canned answers; real audio produces no transcript. With
`--valkey redis://localhost:6379/0 --sqs` and a worker started with
`COPILOT_FIXTURES_DIR=apps/worker/tests/fixtures`, the whole flow (packs, session log,
post-call outputs) runs without any key.

After a session the summary is at `GET /v1/sessions/{id}` once its `status` is `ready` (the
desktop app does not show it yet; that is build step 4b). Post-call work waits up to 10 minutes
for the user's own audio unless the client completes the upload sooner.

### Database roles

Migration 0001 creates the roles from ARCHITECTURE.md (`app_owner`, `app_api`,
`app_gateway`, `app_worker`, `app_system`) as `NOLOGIN`. Migrations hold no passwords. Each
environment grants login separately: Terraform in AWS, and `uv run copilot-dev-roles` locally,
which sets the fixed password `<role>-local` and refuses any host but localhost (`--print`
shows the URLs). The API connects as `app_api`; the workers as `app_worker`, and as
`app_system` only for usage reports, account deletion and the session sweeper. The gateway
does not connect to Postgres yet (memory retrieval comes in phase 2). Every request must run
`select set_config('app.user_id', $1, true)` in its transaction (`copilot_core.db.user_tx`).
Without it, queries on user tables raise an error rather than return rows.

## Protocol codegen

`packages/protocol/protocol.schema.json` is the realtime protocol contract. After editing it,
regenerate all three languages and commit the result:

```sh
pnpm codegen          # writes models.py, src/generated.ts and rust/src/generated.rs
pnpm codegen:check    # exits 1 and prints a diff if the generated files are stale
```

Generated files are never edited by hand. Python uses datamodel-code-generator and
TypeScript uses json-schema-to-typescript; both versions are pinned by the lockfiles. Rust uses
`packages/protocol/scripts/codegen_rs.py`, which turns the client and server unions into serde
tagged enums and the schema's constraints into `Validate` impls; it needs no Rust toolchain, so
`codegen:check` runs anywhere.

## Tests and checks

```sh
uv run pytest                 # all Python tests, including those that need the stack
uv run pytest -m "not stack"  # without the local stack
uv run pytest -m stack        # only the tests against the local stack
pnpm test                     # TypeScript contract tests and the overlay's reducer (vitest)
cargo test --workspace        # Rust: protocol contract, client core, integration tests
cargo clippy --workspace --all-targets && cargo fmt --all --check
pnpm typecheck                # tsc for protocol and desktop
pnpm codegen:check            # generated protocol types are current
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

- **Contract tests** (`packages/protocol/tests/`, `packages/protocol/rust/tests/`): the
  fixtures in `packages/protocol/fixtures/messages.json` round-trip through the Python,
  TypeScript and Rust types. They cover every message type, messages that must be rejected, and audio frame
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
- **Core unit tests** (`packages/core/tests/`): the Cognito verifier with locally generated
  RSA keys (signature, issuer, expiry, `token_use`, app client, `alg` confusion, JWKS caching
  and rotation), envelope encryption (round trip, row and table bound associated data, a data
  key that opens only for its user; KMS calls checked for the `user_id` encryption context),
  and monotonic UUIDv7.
- **API tests** (`apps/api/tests/`, marker `stack`): a throwaway database connected as
  `app_api`, Valkey database 14 and the S3 store. First login, settings, presigned uploads
  through S3 (and a tampered URL refused), ingest, jobs and interviews with their packs,
  sessions with decrypted outputs and transcript, cross-user 404s, the user's audio, usage,
  account deletion, and a Cognito token end to end.
- **Worker tests** (`apps/worker/tests/`, marker `stack`): every job against the stack, with
  Claude and Deepgram replayed from `apps/worker/tests/fixtures` through the real clients
  (see the README there; re-record with `scripts/record_worker_fixtures.py`). Resume to memory
  nodes and digest, job description to job and pack, interview packs and their TTL, stale
  pack rebuilds, the session log flush (including an answer logged before its question),
  session end and billing, the user's audio, post-call outputs, no text at all without
  transcript consent, ciphertext bound to its row and user, `user.delete` leaving no rows, S3
  objects, Valkey keys or data keys, usage reports, the sweeper, the scheduler, and the
  consumer against ElasticMQ: every job type delivered twice runs once, failures and retries,
  visibility heartbeats, and SIGTERM draining.
- **End to end** (`tests/test_end_to_end.py`, marker `stack`): uploads through the API, the
  workers build the pack, a session through the real gateway loads it, the user's audio is
  uploaded, and `GET /sessions/{id}` returns the summary; plus `scripts/dev_profile.py` on the
  same path.
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
- **Desktop client unit tests** (`apps/desktop/core/src/`): resampling, the tick clock, WebRTC
  AEC on a simulated echo, the VAD segmenter, frame encoding and framing (pre-roll, 100 ms
  frames, the empty speech-end frame), the 10 s ring buffer, the reconnect backoff and the
  action for every close code.
- **Audio pipeline test** (`apps/desktop/core/tests/pipeline.rs`): synthesized speech through
  Silero VAD and the framers, and the no-headphones case: the far end leaking into the mic is
  heard as the user without AEC and not with it, while the user's own speech still counts.
- **Client integration tests** (`apps/desktop/core/tests/gateway.rs`, `engine.rs`): the client
  against the real gateway code (`copilot_gateway.offline`, scripted STT and Claude, started
  from `.venv`, so run `uv sync` first) with a fake audio source: questions and both answer
  passes, typed and hotkey questions, expand and feedback, a dropped socket with resume and
  ring resend, `4401` with a token refresh, `4409` and ending the other session, `4410`,
  `auth.expiring`, and a gateway restart, with and without Valkey state. The restart-with-state
  test uses the compose stack's Valkey (database 15) and says it skipped when that is down.
