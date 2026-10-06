# Models

`silero_vad.onnx` is Silero VAD v6.2 (opset 16, 16 kHz and 8 kHz), MIT licensed, from
[snakers4/silero-vad](https://github.com/snakers4/silero-vad) as bundled by the
`silero-vad-rust` 6.2.2 crate. It is the same Silero release that `pysilero-vad` runs in
`copilot-replay` (evals/), so the desktop client and the replay tool segment speech alike.
It is embedded in the binary at build time (`audio/vad.rs`).

SHA-256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`.
