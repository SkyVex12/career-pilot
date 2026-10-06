# Copilot

Desktop career copilot: listens to live interviews and meetings, detects questions aimed at
the user, and shows answers grounded in their career memory. The design is in
[ARCHITECTURE.md](ARCHITECTURE.md), which is the source of truth for everything here.

Status: Stage 0, build step 1. The repo skeleton, realtime protocol, local stack and first
database migration are in place. Services start and answer health checks; they have no
logic yet.

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
evals/          eval sets and runners (empty)
infra/          Terraform (empty)
scripts/        codegen.py
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
