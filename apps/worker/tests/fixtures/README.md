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
| `deepgram/prerecorded.json` | httpx2 | `me_audio.transcribe` (Deepgram `/v1/listen`) |
| `inputs/resume.md`, `inputs/job.md` | | The sample documents the tests upload |

The replay picks the Claude file by the first line of the request's system prompt. Short ids
(`n:3f2a`) only exist at run time, so recordings write the Nth short id of the request's
`<digest>` as `{{ref:N}}` and the replay fills in the run's ids.

**These files were written by hand** in the exact wire format (events, usage, a thinking
block, stop reason), because no provider keys were available when they were made. Re-record
them from the real services with the same requests the workers send:

```sh
uv run python scripts/record_worker_fixtures.py                   # needs ANTHROPIC_API_KEY
uv run python scripts/record_worker_fixtures.py --deepgram me.wav # needs DEEPGRAM_API_KEY
```

The tests check structure and flow, not the wording, so they keep passing on new recordings
as long as the sample documents still yield three stories and at least one cited answer.
