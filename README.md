# Copilot

Desktop career copilot: listens to live interviews and meetings, detects questions aimed at
the user, and shows answers grounded in their career memory. The design is in
[ARCHITECTURE.md](ARCHITECTURE.md), which is the source of truth for everything here.

Status: Stage 0, build step 5. The gateway serves live sessions: the WebSocket protocol,
Deepgram STT on the "them" channel, turn detection, the Haiku detector, fast and deep answers
from the hot digest and session pack, session state and logs in Valkey, and jobs on the queue.
The Windows desktop app captures the call (WASAPI loopback) and the microphone, cancels echo,
runs Silero VAD, streams to the gateway and shows answers in an always-on-top overlay. The
Copilot API takes uploads (resume, job description), jobs, interviews and the user's audio,
and serves sessions with their post-call outputs. The workers turn a resume into the hot digest
and an interview into its session pack, copy the session log into Aurora with per-user
encryption, transcribe the user's audio, and write the summary and feedback. The desktop app
works through the API: it uploads documents, lists jobs, schedules interviews, records the
user's own audio (encrypted, only with transcript consent) and uploads it after the call, and
shows the post-call outputs and the session history. Stage 0 deploys to Google Cloud with
Terraform and GitHub Actions (see "Deploying Stage 0"; the AWS version is kept, unused); there
the app signs in with Google, and locally it uses the dev token. Every service logs JSON lines
with the session id to Cloud Logging.

## Layout

```
apps/
  desktop/      Tauri 2 app (Windows for now)               (pnpm, cargo)
    core/       copilot-client: capture, AEC, VAD, framing, gateway client
    src-tauri/  Tauri shell: windows, commands, hotkey, settings
    src/        React UI: overlay, session and post-call, documents, jobs, history,
                settings, debug
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
infra/gcp/      Terraform for Stage 0 on GCP, and stage.py (see infra/gcp/README.md)
infra/          Terraform for Stage 0 on AWS (unused), and deploy.py (see infra/README.md)
docker/         the service images (python.Dockerfile, proxy/) and the local stack's config
.github/        CI on pull requests; deploy on merge to main; desktop releases on desktop-v* tags
scripts/        codegen.py, dev_profile.py, record_fixtures.py, record_worker_fixtures.py,
                desktop_test_stack.py (the backend for the client's API tests)
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

Logs go to the console as text, with the session id (and user and job) at the end of each line.
`COPILOT_LOG_DIR=logs` also writes them as JSON lines to a file per run and service, the format
CloudWatch gets in AWS (`COPILOT_LOG_FORMAT=json` switches the console too).

### Running the API and workers locally

Both read the same `.env` as the gateway (start from `.env.example`). Besides the dev auth
variables below they need:

| Variable | For |
| --- | --- |
| `COPILOT_API_DATABASE_URL`, `COPILOT_WORKER_DATABASE_URL`, `COPILOT_SYSTEM_DATABASE_URL` | Each service's own role (`app_api`, `app_worker`, `app_system`); the defaults match `copilot-dev-roles`. `COPILOT_DATABASE_URL` is only for migrations |
| `COPILOT_KEY_PROVIDER=dev`, `COPILOT_DEV_MASTER_KEY` | Per-user data keys wrapped by a local master key instead of KMS (dev only; see ARCHITECTURE.md "Security and privacy"). Make one with `uv run python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` |
| `COPILOT_S3_ENDPOINT`, `COPILOT_S3_ACCESS_KEY`, `COPILOT_S3_SECRET_KEY` | The compose S3 store |
| `ANTHROPIC_API_KEY` or Bedrock credentials | Workers: resume extraction (Opus), job descriptions, packs and post-call outputs (Sonnet). On Bedrock they try the Flex tier first, then Standard |
| Or `OPENAI_API_KEY` with `COPILOT_LLM_PROVIDERS=openai` | The same jobs on OpenAI: a model and reasoning effort per job in `COPILOT_OPENAI_<JOB>_MODEL` and `..._REASONING` (extract, job, pack, postcall); `.env.example` has suggestions, nothing is built in. Pack embeddings use `COPILOT_OPENAI_EMBED_MODEL` |
| `DEEPGRAM_API_KEY` | Workers: transcribing the user's own audio after a call. Without it the audio is deleted untranscribed |

`COPILOT_FIXTURES_DIR=apps/worker/tests/fixtures` (dev and test only) makes the worker answer
every model and Deepgram call from the recorded fixtures, for running the whole flow without
keys: OpenAI's recordings when `openai` is the first provider, Claude's otherwise. In AWS the API and workers use `COPILOT_AUTH=cognito` with
`COPILOT_COGNITO_USER_POOL_ID` and `COPILOT_COGNITO_CLIENT_IDS`, and `COPILOT_KEY_PROVIDER=kms`
with `COPILOT_KMS_KEY_ID` (Terraform sets these; see "Deploying Stage 0").

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
| `COPILOT_ENV=dev`, `COPILOT_AUTH=dev`, `COPILOT_DEV_TOKEN` | Dev auth: a static bearer token for `COPILOT_DEV_USER_ID`. It works only with `COPILOT_ENV=dev` and never inside AWS, which signs in through Cognito (see "Deploying Stage 0") |
| `COPILOT_LLM_PROVIDERS` | `anthropic`, `bedrock`, `openai`, or several in fallback order (`openai,anthropic`) |
| `ANTHROPIC_API_KEY` | The direct Claude API |
| AWS credentials or `AWS_BEARER_TOKEN_BEDROCK` | Claude on Bedrock (Mantle), and Cohere embeddings when `COPILOT_EMBEDDER=bedrock` |
| `OPENAI_API_KEY` | OpenAI, with `COPILOT_OPENAI_DETECTOR_MODEL`, `..._FAST_MODEL`, `..._DEEP_MODEL` and their `..._REASONING` (see `.env.example`), its own first-token timeouts (`COPILOT_OPENAI_FIRST_TOKEN_TIMEOUT_MS`, `COPILOT_OPENAI_DEEP_FIRST_TOKEN_TIMEOUT_MS`), and OpenAI embeddings for prepared answers (`COPILOT_OPENAI_EMBED_MODEL`) |
| `COPILOT_MODEL_PRICES` | Prices per million tokens for `answers.cost_usd`. Claude's are built in; set the OpenAI models' (JSON, see `.env.example`). The gateway logs models without a price at startup |
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
   `COPILOT_DEV_TOKEN` of 16+ characters, `COPILOT_DEV_MASTER_KEY`, model credentials
   (`ANTHROPIC_API_KEY` with `COPILOT_LLM_PROVIDERS=anthropic`, or Bedrock, or
   `OPENAI_API_KEY` with `COPILOT_LLM_PROVIDERS=openai` and the `COPILOT_OPENAI_*` model
   settings and `COPILOT_MODEL_PRICES` from `.env.example`) and `DEEPGRAM_API_KEY`. Leave
   `COPILOT_FIXTURES_DIR` unset for real models and Deepgram.
3. **API, worker, gateway:** `uv run copilot-api`, `uv run copilot-worker` and
   `uv run copilot-gateway`, each in its own terminal (and `uv run copilot-scheduler` for the
   timed jobs, optional locally). Bind `HOST=0.0.0.0` only if the app runs on another machine.
4. **App:** `pnpm --filter @copilot/desktop tauri dev`. The first build takes a few minutes.
5. **Settings** (in the app): Environment: Local, the gateway URL
   (`ws://127.0.0.1:8081/v1/session`), the API URL (`http://127.0.0.1:8080/v1`), the dev token
   (stored in the Windows Credential Manager; the same token signs in to both), the microphone, and "Call audio from": the speakers or
   headset the call app plays on. Leave echo cancellation on. Save.
6. **Documents:** upload your resume and a job description. Each shows Processing, then
   Processed; the career digest shows Ready once the resume's facts are in, and the job
   description's job shows its pack.
7. **Jobs:** schedule an interview for the job (round and start time). It builds a pack for
   that round. A pack lives until a day after its interview.
8. **Session:** pick the mode, the interview (or a job) under "Call for", the call app and the
   transcript consent, then Start session. The overlay opens on top; drag it by its header. The
   hotkey (Ctrl+Shift+Space by default) answers the last thing said when detection missed it;
   the box at the bottom of the overlay takes a typed question. Ctrl+Shift+D in the main window
   shows the debug panel: partial transcripts, detected questions, and latency per question
   (end of speech to first answer word, on the client, next to the gateway's `latency_ms`).
9. **After the call:** End session. The post-call screen follows the upload of your own audio
   and polls the API until the summary, feedback, follow-up email (or standup report) and the
   transcript are ready, for up to 5 minutes. History lists past sessions, this month's live
   minutes, and account deletion.

`scripts/dev_profile.py --resume resume.pdf --job job.md --round technical` does steps 6 and 7
from the command line.

With transcript consent the app records your own audio (the microphone after echo
cancellation) in `%LOCALAPPDATA%\com.copilot.desktop\me-audio`, encrypted with a key kept in
the Windows Credential Manager (`com.copilot.desktop`, `me-audio-key`). It uploads it when the
call ends, retries for about 11 minutes, deletes it once the upload is complete, and retries
whatever is left at the next start. Without consent nothing is recorded. Post-call work waits
up to 10 minutes for the upload.

To check the devices without the app, `cargo run -p copilot-client --example capture_check -- 5`
lists them and reports how much audio each stream delivered (play something meanwhile;
loopback is silent otherwise).

To work on the UI without provider keys, run the gateway with scripted STT and Claude instead
of `copilot-gateway`: `uv run python -m copilot_gateway.offline --port 8081 --token <dev token>`.
Typed questions get canned answers; real audio produces no transcript. With
`--valkey redis://localhost:6379/0 --sqs` and a worker started with
`COPILOT_FIXTURES_DIR=apps/worker/tests/fixtures`, the whole flow (packs, session log,
post-call outputs) runs without any key; documents then come back as the recorded sample's.

### End-to-end test with a teammate

A real call from a fresh stack, with real models (Claude or OpenAI) and Deepgram. You need a teammate on a call
app (Zoom, Meet or Teams) who agrees to be recorded and plays the interviewer; about 30
minutes. Use your own resume (PDF or Markdown) and a real job description saved as a PDF,
Markdown or text file.

1. **Fresh stack.** `docker compose down -v`, then `docker compose up -d --wait`,
   `uv run copilot-migrate`, `uv run copilot-dev-roles`.
2. **`.env`** as in step 2 above, with `ANTHROPIC_API_KEY` and
   `COPILOT_LLM_PROVIDERS=anthropic` (or `OPENAI_API_KEY`, `COPILOT_LLM_PROVIDERS=openai` and
   the OpenAI settings from `.env.example`) and `DEEPGRAM_API_KEY`, and without
   `COPILOT_FIXTURES_DIR`.
3. **Services**, one terminal each: `uv run copilot-api`, `uv run copilot-worker`,
   `uv run copilot-gateway`, `uv run copilot-scheduler`. Check
   `curl http://127.0.0.1:8080/healthz` and the same on ports 8081 and 8082. Keep the worker
   terminal visible: every job logs a line.
4. **App.** `pnpm --filter @copilot/desktop tauri dev`. In Settings: gateway URL, API URL, the
   dev token from `.env`, your microphone, and the output device the call plays on. If you
   have no headphones, keep echo cancellation on. Save. History is empty after `down -v`: the
   database is new.
5. **Upload the resume** under Documents. Expect Processing, then Processed and "Career
   digest: Ready" within about 1 to 2 minutes (Opus, or the OpenAI extract model, reads the
   resume; the worker logs `memory.extract`, then `digest.rebuild`).
6. **Upload the job description.** Expect Processing, then Processed with the company and
   title read from it, and "Pack ready (N answers)" a minute later (`pack.build`).
7. **Schedule the interview** under Jobs: the job, the round you will play (technical or
   system design works best), and a start time about 15 minutes out. Expect "Pack ready" in
   the Interviews list within about a minute.
8. **Join the call** with your teammate, on the output device you picked. Under Session: mode
   interview, "Call for" the interview you scheduled, the call app, and tick the transcript
   consent (you both agreed). Start session; the overlay opens.
9. **Run the interview** for 10 to 15 minutes. Ask your teammate for a mix: one behavioral
   ("Tell me about a time..."), two technical, one system design, plus small talk and a
   question for someone else ("Sam, what do you think?"), which should not fire. Answer
   aloud. Expect talking points about a second after each question ends (on OpenAI about
   2 to 3 seconds: see "OpenAI as a provider" in ARCHITECTURE.md), and a deeper answer below
   for behavioral, technical and system design ones. Try the hotkey once after
   a question the overlay missed, and type one question into the overlay. Open the debug
   panel (Ctrl+Shift+D) to see the latency per question.
10. **End the session.** The post-call screen shows "Waiting for your audio", then "Your audio
    is uploaded", then the summary, feedback and follow-up email (Copy puts it on the
    clipboard), and the transcript with both sides. Expect this within about 3 minutes.
11. **History.** The session is listed with its minutes; opening it shows the same outputs.
    The live minutes at the top include it.
12. **Optional checks.** A short session without transcript consent: no recording appears in
    the `me-audio` folder, and the summary covers only length and question types. Close the
    app right after ending a consented session (before the upload): at the next start the
    recording uploads and the session still gets its transcript, if that start is within 10
    minutes of the end. Delete a session from History. Delete the account last (type
    "delete"): History is empty afterwards.

Things worth sending back: the debug panel's latency table, questions the overlay missed or
answered when it should not have, anything wrong in the outputs, the provider you ran on, and
the worker log around any failure.

**On OpenAI without the app**, the same path from the command line (API, worker and gateway
running with `COPILOT_LLM_PROVIDERS=openai`): `scripts/dev_profile.py` as above, then a
session with typed `question.force` messages through `copilot_gateway.testing.ProtocolClient`
(`ws://127.0.0.1:8081/v1/session`, the dev token), `answer.expand` for the deep pass,
`session.end`, the user's audio through `POST /sessions/{id}/me-audio` and `.../complete`, and
`GET /sessions/{id}` until it is `ready`. The gateway logs `prepared-answer matching on:
openai:...` at startup and opens its OpenAI connection (`GET /v1/models`) before the first
question.

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

## Deploying Stage 0

Stage 0 runs on Google Cloud, in project `career-pilot-512115` (us-central1), on a single
Compute Engine VM: gateway, API, worker, an nginx proxy, Postgres with pgvector, Valkey and
ElasticMQ (the SQS-compatible jobs queue) run as containers on Container-Optimized OS, with
their data on a separate persistent disk, next to Cloud Storage, Cloud KMS, Secret Manager and
Cloud Logging (ARCHITECTURE.md, "Stage 0 on GCP"; the layout is in `infra/gcp/README.md`). It
costs about $15 a month running all the time and about $10 stopped between tests, plus OpenAI
and Deepgram per use. The AWS version (`infra/envs/stage0`) is kept but unused: the AWS
account's organization policy blocks it (`infra/README.md`).

**No TLS in Stage 0.** The app talks to the VM over plain HTTP and WebSocket on its IP. Your
sign-in token and call text cross the internet unencrypted on that hop; the settings screen
says so. Port 80 can be limited to an IP allow-list (`stage.py allow`); it is open to every
address at the moment, so the Google sign-in allow-list is what keeps strangers out. Document
and audio uploads go straight to Cloud Storage over HTTPS. TLS (a domain and a certificate) is
required before anyone outside the team uses it.

You need the Google Cloud CLI (`winget install Google.CloudSDK`), Terraform 1.11+, `gh` and
`uv`. Every command below runs from the repository root; `stage.py` is
`uv run infra/gcp/scripts/stage.py`.

### First-time setup

Done once, by hand, in this order.

1. **Sign in to Google Cloud.** `gcloud auth login`, then `gcloud auth application-default
   login` (tick every permission on the consent page) and `gcloud auth application-default
   set-quota-project career-pilot-512115`. Terraform uses the second; the third lets the
   Billing Budgets API run.
2. **APIs Terraform needs first:** `gcloud services enable cloudresourcemanager.googleapis.com
   --project career-pilot-512115` (Terraform turns on the rest).
3. **State bucket**, once per project:

   ```sh
   terraform -chdir=infra/gcp/bootstrap init
   terraform -chdir=infra/gcp/bootstrap apply     # copilot-tfstate-career-pilot-512115
   ```

4. **Google sign-in client** (Terraform cannot make it; there is no API for desktop OAuth
   clients). In the Console for the project: Google Auth Platform → Branding (an app name and
   your email), Audience → External, then **Publish app** (while it is in Testing, Google
   expires refresh tokens after 7 days; openid, email and profile need no verification) →
   Clients → Create client → **Desktop app**. Put its client ID in
   `infra/gcp/envs/stage0/terraform.tfvars` (`google_oauth_client_id`); keep the secret for
   step 7.
5. **Allow-lists.** In `infra/gcp/envs/stage0/allowlist.auto.tfvars` (gitignored): the Google
   accounts that may sign in, and the addresses that may reach port 80 (or
   `open_to_internet = true`):

   ```hcl
   allowed_emails   = ["you@gmail.com", "alex@gmail.com"]
   allowed_cidrs    = ["203.0.113.7/32", "198.51.100.20/32"]
   open_to_internet = false
   ```

6. **The stage.** Review the plan before applying:

   ```sh
   terraform -chdir=infra/gcp/envs/stage0 init -backend-config="bucket=copilot-tfstate-career-pilot-512115"
   terraform -chdir=infra/gcp/envs/stage0 plan -out=tfplan
   terraform -chdir=infra/gcp/envs/stage0 apply tfplan
   terraform -chdir=infra/gcp/envs/stage0 output      # IP, URLs, CI variables
   ```

   The VM boots, mounts the data disk and starts Postgres, Valkey and ElasticMQ; the app
   waits for the first deploy.
7. **Secrets** (created empty). Generated ones first: the database passwords and the uploads
   HMAC key go straight into Secret Manager, never to the screen. The organization forbids
   service account keys, HMAC keys included, so `--lift-key-policy` lifts that policy for this
   project around the one call and puts it back (it needs Organization Administrator; see
   `infra/gcp/README.md`). Then the three only you have, with a hidden prompt:

   ```sh
   uv run infra/gcp/scripts/stage.py secrets generate --lift-key-policy
   uv run infra/gcp/scripts/stage.py secrets set openai-api-key
   uv run infra/gcp/scripts/stage.py secrets set deepgram-api-key
   uv run infra/gcp/scripts/stage.py secrets set google-oauth-client-secret
   uv run infra/gcp/scripts/stage.py secrets status
   ```

8. **CI/CD.** Repository variables (Settings → Secrets and variables → Actions → Variables, or
   `gh variable set`) from `terraform output ci`: `GCP_WORKLOAD_IDENTITY_PROVIDER`,
   `GCP_CI_SERVICE_ACCOUNT`, `GCP_DEPLOY_SERVICE_ACCOUNT`, `GCP_RELEASE_SERVICE_ACCOUNT`,
   `GCP_REGISTRY` and `GCP_RELEASES_BUCKET`. No key is stored in GitHub: runs sign in through
   Workload Identity Federation, and only from `main` (images, deploys) or a `desktop-v*` tag
   (desktop releases), each as an account that may do only that (`infra/gcp/modules/ci`).
   For desktop releases, also the secrets `TAURI_SIGNING_PRIVATE_KEY` and
   `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` ("Releasing the desktop app").
9. **First deploy:** merge to `main`. The workflow checks, builds and deploys.
10. **The desktop app.** Teammates install the release build ("Installing the desktop app"),
    which knows the stage. A dev build (`tauri dev`): Settings → Environment: Cloud, paste the
    VM's IP (`terraform output external_ip`) into "Cloud host", press Fill URLs, Save, then
    Sign in: the browser opens Google's sign-in, and the app keeps the sign-in in the Windows
    Credential Manager.

### Deploy

Every push to `main` deploys (`.github/workflows/deploy.yml`):

1. **Checks:** the same jobs as on a pull request (`ci.yml`: Python, TypeScript, Rust on Linux
   and Windows, Terraform). Nothing below runs unless all of them pass.
2. **Images:** gateway, API, worker and proxy for linux/amd64 and linux/arm64, one tag, the git
   SHA, in Artifact Registry.
3. **Deploy** of exactly those images, as the deploy service account (Workload Identity
   Federation; IAP and OS Login to the VM, no SSH port): `stage.py deploy --tag <sha> --start
   --check`. If the VM is stopped it is started first and left running (the run's summary
   says so). On the VM, `copilotctl` pulls the images, runs the migrations and the database
   logins as a one-off container, then replaces the API, the worker, the gateway and the proxy
   one at a time; each must report healthy, or it puts the previous tag back. Then the health
   check (`stage.py health`): each service's `/healthz`, and through nginx the API's
   `/v1/auth/config` and a WebSocket handshake on `/v1/session` that the gateway itself
   answers. If it fails, the previous tag is deployed again and the run fails.

One deploy runs at a time: a merge that lands during a deploy waits for it, and of several
waiting the newest wins (the older ones show as cancelled). A call in progress reconnects and
resumes in a few seconds.

**Watching a deploy:** Actions → deploy, or `gh run watch` (`gh run list --workflow deploy`).
The run's summary page shows the commit, the tag it replaced, the health check, and whether
the VM was started or a rollback happened. Then `stage.py status` and `stage.py logs --since
15m`.

**Deploying a specific commit** (it must be on `main`): Actions → deploy → Run workflow, with
the SHA, or `gh workflow run deploy --ref main -f sha=<git sha>`. That run checks out the
commit, runs the checks on it, builds its images (mostly from the cache) and deploys it, with
the same health check and rollback. By hand, without the checks, for images already in the
registry:

```sh
uv run infra/gcp/scripts/stage.py deploy --tag <git sha> --check   # --start if it is stopped
uv run infra/gcp/scripts/stage.py status
uv run infra/gcp/scripts/stage.py health
```

**Branch protection.** `main` should require a pull request with every CI job passing; GitHub
offers that for private repositories only on a paid plan (Pro, Team or Enterprise), so it is
not turned on. Until then, pushing to `main` directly still runs the checks before anything is
deployed. With a paid plan: Settings → Branches → Add rule for `main` (or `gh api -X PUT
repos/SkyVex12/career-pilot/branches/main/protection`), "Require a pull request", "Require
status checks": the jobs of `ci` (python, typescript, rust-linux, rust-windows, terraform).

Infrastructure changes are not deployed by CI: run `terraform plan` and `apply` after merging.
A change to a service's configuration (`copilot-env` in the VM's metadata) or to a secret is
live after an apply and `stage.py restart` (each container reads both when it starts).

### Rollback

A failed health check rolls back by itself. By hand:

```sh
uv run infra/gcp/scripts/stage.py rollback                       # the tag deployed before this one
uv run infra/gcp/scripts/stage.py deploy --tag <git sha> --check # or any older tag still kept
```

or Actions → deploy → Run workflow with the older commit's SHA (checks and all). A rollback
does not revert `main`: the next merge deploys the head of `main` again, so revert the bad
commit in a pull request too. The registry keeps about the last ten tags. Migrations follow
expand then contract, so older code runs against the newer schema.

### Logs

Every container logs to Cloud Logging (log `cos_containers` of the VM, kept 30 days). The
Python services write JSON lines, and every line about a session carries its `session_id`, so
Cloud Logging indexes it as `jsonPayload.session_id`.

```sh
uv run infra/gcp/scripts/stage.py logs --session <session id> --since 1d   # every service
uv run infra/gcp/scripts/stage.py logs --service gateway --since 30m
uv run infra/gcp/scripts/stage.py logs --errors --since 1d
```

In the Console: Logging → Logs Explorer, with
`logName="projects/career-pilot-512115/logs/cos_containers" AND jsonPayload.session_id="<id>"`.
A shell on the VM: `stage.py ssh` (IAP and OS Login; no SSH port is open), then
`sudo docker logs copilot-gateway` or `sudo bash /var/lib/copilot/copilotctl.sh status`.

### Stopping between tests

Stopping the VM stops the whole app; the disks, the reserved IP and the small fixed items keep
billing (about $10 a month, $7 of it the IP while it is not in use).

```sh
uv run infra/gcp/scripts/stage.py stop
uv run infra/gcp/scripts/stage.py start     # the services are up again in 2 to 3 minutes
```

While it is stopped, the scheduler and the nightly backup do not run, and deploys fail.

### Backups and restore

`copilotctl backup` runs every night at 08:30 UTC: a `pg_dump` of the database to the backups
bucket, kept 7 days (uploads are in Cloud Storage already; Valkey and the queue are live state).

```sh
uv run infra/gcp/scripts/stage.py backup                 # one now
uv run infra/gcp/scripts/stage.py backups                # what the bucket has
uv run infra/gcp/scripts/stage.py restore postgres/copilot-20261020T083000Z.dump
```

A restore stops the app, runs the migrations (so the roles exist on a new disk), restores the
dump over the database with `pg_restore --clean`, sets the roles' logins again and starts the
app. Restoring onto a new VM or disk: apply, `secrets` as before (the database passwords stay
in Secret Manager), deploy, then restore.

### The allow-list

```sh
uv run infra/gcp/scripts/stage.py allow --show
uv run infra/gcp/scripts/stage.py allow --me 198.51.100.20/32   # replace: this machine and one more
uv run infra/gcp/scripts/stage.py allow --any                   # every address (no TLS!)
```

It changes the firewall rule at once; Terraform sets it only when it creates the rule. Who may
sign in is `allowed_emails` in `allowlist.auto.tfvars`: change it, then `terraform apply` and
`stage.py restart`.

### Tearing it all down

```sh
uv run infra/gcp/scripts/stage.py backup && gcloud storage cp "gs://copilot-stage0-backups-career-pilot-512115/postgres/*" .   # optional
terraform -chdir=infra/gcp/envs/stage0 destroy
terraform -chdir=infra/gcp/bootstrap destroy        # the state bucket, last
```

This deletes the VM and its disks, the uploads and backups buckets, the images, the secrets
and CI's access. The KMS key's versions are destroyed a day later; the empty key ring stays,
since Google never deletes key rings (it costs nothing, and a new apply needs
`terraform import` of it or another `name`). Delete the repository variables so the workflow
skips the GCP jobs again.

## Installing the desktop app

For teammates (Windows 10 or 11). The installer is built from a `desktop-v*` tag and points at
the Stage 0 server; it updates itself.

1. **Get on the allow-list first.** Only listed Google accounts can sign in. Send the owner the
   Google account email you will use; they add it to `allowed_emails` in
   `infra/gcp/envs/stage0/allowlist.auto.tfvars` and run:

   ```sh
   terraform -chdir=infra/gcp/envs/stage0 plan -out=tfplan    # shows only copilot-env changing
   terraform -chdir=infra/gcp/envs/stage0 apply tfplan
   uv run infra/gcp/scripts/stage.py restart api gateway worker
   ```

2. **Download** the installer:
   <https://storage.googleapis.com/copilot-stage0-releases-career-pilot-512115/desktop/Copilot-setup.exe>
3. **Run it.** The installer is not code-signed yet, so Windows warns: "Windows protected your
   PC". Click **More info**, then **Run anyway**. (Your browser may also ask whether to keep
   the file: choose Keep.) It installs for your user only, without an administrator prompt,
   and adds Copilot Desktop to the Start menu.
4. **Sign in.** Open Copilot Desktop → Settings → **Sign in**. Your browser opens Google's sign-in;
   choose the allow-listed account and allow access. "Signed in as you@…" appears in the app.
   An account not on the list gets "not allowed" from the server.
5. Settings: pick the microphone and the speakers the call plays on.

**Updates:** at each start the app asks the releases bucket for a newer version; when there is
one, a bar at the top offers **Install and restart** (not during a call). Updates are signed;
the app installs only installers signed with the release key.

**Plain HTTP:** Stage 0 has no TLS yet (see "Deploying Stage 0"): use it only for the team's
tests. To uninstall: Settings → Apps → Copilot Desktop → Uninstall; the sign-in stays in the Windows
Credential Manager (`com.copilot.desktop` entries) until you sign out.

## Releasing the desktop app

Tag a commit on `main`:

```sh
git tag desktop-v0.2.0 origin/main && git push origin desktop-v0.2.0
gh run watch $(gh run list --workflow release-desktop -L 1 --json databaseId -q '.[0].databaseId')
```

`.github/workflows/release-desktop.yml` builds the installer on a Windows runner with the
tag's version (`pnpm tauri build --features release`), signs it for the updater, and publishes
to the public bucket `copilot-stage0-releases-career-pilot-512115`: `desktop/<version>/`
(the installer and its signature, kept), `desktop/Copilot-setup.exe` (the download link) and,
last, `desktop/latest.json`, the update manifest. Installed apps pick it up at their next
start. Versions must go up (semver); a tag on a commit not on `main` builds but does not
publish.

**Release builds.** The `release` Cargo feature embeds `apps/desktop/release.json` (the stage's
API and gateway URLs, the update manifest's URL and the updater's public key): no local
environment, no dev token, no URL fields in the settings. After a change to the stage's IP or
bucket, `uv run infra/gcp/scripts/stage.py desktop-config` rewrites the URLs from `terraform
output desktop`; commit the file and tag a release. `apps/desktop/scripts/release.mjs` writes
the Tauri config for the build (`config`) and the manifest (`manifest`). A local release build:

```sh
cd apps/desktop
node scripts/release.mjs config --version 0.2.0
$env:TAURI_SIGNING_PRIVATE_KEY = "$HOME\.copilot-release\updater.key"          # PowerShell
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = Get-Content "$HOME\.copilot-release\updater.key.password"
pnpm tauri build --features release --config src-tauri/tauri.release.conf.json
node scripts/release.mjs manifest --version 0.2.0 --out ../../dist-release
```

**The updater key.** `pnpm tauri signer generate -w <file>` made the pair. The public key is in
`release.json`; the private key and its password are not in the repository: they are the
secrets `TAURI_SIGNING_PRIVATE_KEY` (the key file's contents) and
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, with the owner's copy kept offline. Losing the private
key means installed apps can no longer update: a new key needs a new installer, installed by
hand. If it leaks, make a new pair, release with the new public key, and have everyone
reinstall.

### Code signing

The installer is unsigned for now, hence the SmartScreen warning. Signing is a drop-in in
`release-desktop.yml`: set the repository variable `AZURE_SIGNING_ACCOUNT` (with
`AZURE_SIGNING_PROFILE`, `AZURE_SIGNING_ENDPOINT` and the secrets `AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`) for Azure Trusted Signing, or `WINDOWS_SIGN_COMMAND`
for any other signer (Tauri runs it with `%1` as the file to sign, for the app and the
installer). The run's summary shows the Authenticode status. Options, at list prices as of
late 2026 (check before buying):

| Option | Cost | Notes |
| --- | --- | --- |
| Azure Trusted Signing (Artifact Signing) | about $10 a month (Basic, 5,000 signatures) | Microsoft-managed keys and short-lived certificates, built for CI. Identity validation of an organization (or, in the US and Canada, an individual); takes days. Simplest with this workflow |
| OV code signing certificate (Sectigo, DigiCert, SSL.com, …) | about $200 to $500 a year | Since 2023 the key must live in a hardware token or a cloud HSM, so a `.pfx` in a GitHub secret is no longer possible; use the CA's cloud signing (e.g. SSL.com eSigner, DigiCert KeyLocker, extra fees) or Azure Key Vault Premium with `WINDOWS_SIGN_COMMAND` |
| EV code signing certificate | about $300 to $700 a year | The same key rules. Since 2024 EV no longer skips SmartScreen's reputation check, so it buys little over OV here |

With any of them, SmartScreen may still warn for the first downloads until the publisher
builds reputation; signing removes "Unknown publisher" at once.

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
pnpm test                     # TypeScript contract tests and the desktop UI's logic (vitest)
cargo test --workspace        # Rust: protocol contract, client core, integration tests
                              # (tests/api.rs needs the local stack)
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
- **Logs and measurements:** `packages/core/tests/test_logs.py` (JSON lines with the bound ids,
  ids carried into asyncio tasks, the file per run, JSON inside AWS),
  `apps/gateway/tests/test_measurements.py` (every `latency_ms` stage, the route kept with the
  answer, prepared-answer similarity on a miss, the expand tap, `latency.report` before and after
  the answer is logged, and the session id on every gateway line), the flush merging late
  timings (`apps/worker/tests/test_session_log.py`), and the API's request lines.
- **AWS pieces:** `copilot-db-logins` against the stack (`packages/core/tests/test_dblogins.py`),
  `user.delete` deleting the Cognito user, `GET /v1/auth/config`, the settings refusing dev and
  test on AWS compute, and the desktop's Cognito sign-in (`apps/desktop/core/src/cognito.rs`:
  PKCE, the loopback redirect, refresh, and a revoked sign-in) against a fake token endpoint.
- **Core unit tests** (`packages/core/tests/`): the Cognito verifier with locally generated
  RSA keys (signature, issuer, expiry, `token_use`, app client, `alg` confusion, JWKS caching
  and rotation), envelope encryption (round trip, row and table bound associated data, a data
  key that opens only for its user; KMS calls checked for the `user_id` encryption context),
  answer pricing (model names across providers, cache reads, tiers, configured prices),
  OpenAI request content (a PDF as `input_file`), the prefix cache key, OpenAI embeddings at
  1024 dimensions, and monotonic UUIDv7.
- **API tests** (`apps/api/tests/`, marker `stack`): a throwaway database connected as
  `app_api`, Valkey database 14 and the S3 store. First login, profile and settings each on
  their own path, presigned uploads through S3 (and a tampered URL refused), ingest and its
  status, the digest in the document list, jobs and interviews with their packs, sessions
  with decrypted outputs and transcript, cross-user 404s, the user's audio (refused while the
  session is live), usage, account deletion, and a Cognito token end to end.
- **Worker tests** (`apps/worker/tests/`, marker `stack`): every job against the stack, with
  Claude and Deepgram replayed from `apps/worker/tests/fixtures` through the real clients
  (see the README there; re-record with `scripts/record_worker_fixtures.py`, and
  `--provider openai` for OpenAI's). `test_openai_jobs.py` runs extraction, the job
  description, the pack and post-call outputs on OpenAI only, and checks that pack vectors
  name their embedding model and are rebuilt when it changes. Resume to memory
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
- **Provider fixtures** (`apps/gateway/tests/fixtures/`): the Anthropic SDK stream, OpenAI
  Responses API streams and the Deepgram WebSocket are replayed from wire-format recordings,
  so no test needs the network. `uv run python scripts/record_fixtures.py --wav clip.wav`
  re-records them with real keys (`--only openai` for OpenAI's). `test_openai.py` covers
  streaming with the same prompt blocks, the detector's structured output, a refusal, a
  failure mid-stream falling back to Claude, and model, tokens and cost through a session on
  OpenAI only.
- **Replay tool tests** (`evals/tests/`): `copilot-replay` against an in-process gateway,
  with and without a dropped socket.
- **Desktop client unit tests** (`apps/desktop/core/src/`): resampling, the tick clock, WebRTC
  AEC on a simulated echo, the VAD segmenter, frame encoding and framing (pre-roll, 100 ms
  frames, the empty speech-end frame), the 10 s ring buffer, the reconnect backoff and the
  action for every close code. The API client (error details, allowed calls) and the user's
  audio: the per-install key in the credential store, the encrypted file round trip to WAV,
  no plaintext on disk, the wrong key and altered or reordered chunks refused, a torn last
  chunk after a crash, a new session mid-call, nothing recorded without consent, and the
  upload logic against a fake API: success deletes the local copy, network errors and a live
  session keep it and the retry succeeds, a failure stays for the next start, final refusals
  and unreadable files delete it.
- **Desktop UI tests** (`apps/desktop/tests/`): the overlay's reducer, the post-call polling
  (ready, the 5-minute limit, errors while polling), the outputs' order per mode, the
  transcript, and the start screen's interview and job choices.
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
- **Client API integration test** (`apps/desktop/core/tests/api.rs`, needs the local stack):
  the client against the real API and a real worker consumer (Claude and Deepgram on the
  recorded fixtures) and the gateway, which `scripts/desktop_test_stack.py` runs on a
  throwaway database and queue. Documents through the presigned uploads, the digest and
  packs, an interview and its pack, a call with the user's audio recorded, an upload that
  fails and is retried as at the next start, the post-call outputs with the interview linked
  and both sides in the transcript, a call without consent (nothing recorded, a metadata
  summary), history, usage, and deleting a session and the account. It fails with the reason
  when the stack is down.
