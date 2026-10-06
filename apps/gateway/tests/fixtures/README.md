# Recorded provider fixtures

Wire-format fixtures that the unit tests replay through the real provider adapters, so
`uv run pytest -m "not stack"` needs no network.

| File | Replayed through | Source |
| --- | --- | --- |
| `anthropic_fast.sse` | `AnthropicProvider` (official SDK) over a mock HTTP transport | Streaming Messages API events |
| `deepgram_session.json` | `DeepgramSTT` against a local WebSocket server | Deepgram live `/v1/listen` messages |

`scripts/record_fixtures.py` re-records both from the real services (needs `ANTHROPIC_API_KEY`
and `DEEPGRAM_API_KEY`). Recordings hold no secrets; they do hold the transcript of the clip
used, so record only audio everyone in it agreed to share.
