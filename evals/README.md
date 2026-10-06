# evals

Eval sets, the recorded call corpus and their runners (ARCHITECTURE.md, "Testing and evals").
Recorded calls hold people's voices: only add recordings that every participant agreed to,
and keep raw audio out of git.

## copilot-replay

Streams a WAV file through the gateway in real time as the "them" channel, the way the
desktop client would: Silero VAD (`pysilero-vad`, no torch) decides speech start and end,
only speech is sent in 100 ms frames, with a 200 ms pre-roll and a speech-end frame after
`--min-silence-ms` of silence.

```sh
uv run copilot-replay clip.wav [--url ws://127.0.0.1:8081/v1/session] [--json run.json]
```

It prints the transcript, detected questions and streamed answers as they arrive, then a
table per question and the p50/p95 of each stage:

| Column | Meaning |
| --- | --- |
| `vad` | VAD hangover: true end of speech to the speech-end frame (client side) |
| `det` | Detector call (server, `latency_ms.detector_ms`) |
| `turn` | Speech-end frame to the question being confirmed (server) |
| `ttft` | Fast-pass request to its first token (server) |
| `srv1st` | Speech-end frame to the first `answer.delta` sent (server) |
| `1st word` | True end of speech to the first `answer.delta` received (client), in seconds |
| `deep1st` | True end of speech to the first deep-pass delta, in seconds |

First-word p50/p95 is compared with the 0.9 s / 1.5 s target. `--drop-at SECONDS` aborts the
socket mid-call and resumes with the 10 s ring buffer, as the desktop client does. This is the
replay tool for the recorded call corpus.
