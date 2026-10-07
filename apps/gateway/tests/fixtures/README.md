# Recorded provider fixtures

Wire-format fixtures that the unit tests replay through the real provider adapters, so
`uv run pytest -m "not stack"` needs no network.

| File | Replayed through | Source |
| --- | --- | --- |
| `anthropic_fast.sse` | `AnthropicProvider` (official SDK) over a mock HTTP transport | Streaming Messages API events |
| `openai_fast.sse` | `OpenAIProvider` (official SDK) over a mock HTTP transport | Responses API events of a fast pass on its second call, so it shows a prompt cache hit |
| `openai_deep.sse` | `OpenAIProvider` | A deep pass with reasoning (a reasoning item before the message) |
| `openai_detector.sse` | `OpenAIProvider` | A detector call with a strict JSON schema |
| `openai_refusal.sse` | `OpenAIProvider` | A refusal: refusal deltas and a `refusal` content item. Built from the recorded events' shapes, since a refusal cannot be provoked on demand |
| `openai_failed.sse` | `OpenAIProvider` | `response.created`, `response.in_progress`, then `response.failed` (`server_error`): a failure mid-stream before any text. Built like the refusal |
| `deepgram_session.json` | `DeepgramSTT` against a local WebSocket server | Deepgram live `/v1/listen` messages |

`scripts/record_fixtures.py` re-records the Anthropic, OpenAI and Deepgram files from the real
services (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` with the `COPILOT_OPENAI_*_MODEL` settings,
`DEEPGRAM_API_KEY`); the two built OpenAI files stay as they are. The tests derive their
expectations from the files, so they keep passing on new recordings. Recordings hold no
secrets; they do hold the transcript of the clip used, so record only audio everyone in it
agreed to share.
