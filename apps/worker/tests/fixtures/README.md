# Worker provider fixtures

Wire-format responses that the worker tests (and a worker started with
`COPILOT_FIXTURES_DIR`) replay through the real clients over a mock transport
(`copilot_worker.fixtures`), so no test needs the network or provider keys.

| File | Replayed through | For |
| --- | --- | --- |
| `claude/extract.sse` | Anthropic SDK stream | `memory.extract` of a resume (Opus) |
| `claude/job.sse` | Anthropic SDK stream | `memory.extract` of a job description |
| `claude/pack.sse` | Anthropic SDK stream | `pack.build` for an interview |
| `claude/postcall_{interview,standup,meeting}.sse` | Anthropic SDK stream | `session.postcall` per mode |
| `openai/*.sse` (the same six jobs) | OpenAI SDK stream (Responses API) | The same jobs on OpenAI (`test_openai_jobs.py`), recorded from the real API in October 2026 with the models `.env.example` suggests |
| `deepgram/prerecorded.json` | httpx2 | `me_audio.transcribe` (Deepgram `/v1/listen`) |
| `inputs/resume.md`, `inputs/job.md` | | The sample documents the tests upload |

The replay picks the file by the first line of the request's system prompt (Claude's
`system`, OpenAI's developer message). Short ids
(`n:3f2a`) only exist at run time, so recordings write the Nth short id of the request's
`<digest>` as `{{ref:N}}` and the replay fills in the run's ids.

**The Claude files were written by hand** in the exact wire format (events, usage, a thinking
block, stop reason), because no provider keys were available when they were made. Re-record
them from the real services with the same requests the workers send:

```sh
uv run python scripts/record_worker_fixtures.py                   # needs ANTHROPIC_API_KEY
uv run python scripts/record_worker_fixtures.py --provider openai # needs OPENAI_API_KEY
uv run python scripts/record_worker_fixtures.py --deepgram me.wav # needs DEEPGRAM_API_KEY
```

OpenAI streams a few characters per delta, so a short id can span deltas. For the pack, the
recorder moves delta boundaries out of short ids before writing them as `{{ref:N}}`: the same
events and text, other boundaries. OpenAI models also write ranked story ids without the `n:`
prefix; the pack builder accepts both, but the recorded bare ids are the recording run's, so
the replayed pack ranks its stories in their stored order.

The tests check structure and flow, not the wording, so they keep passing on new recordings
as long as the sample documents still yield three stories and at least one cited answer.
