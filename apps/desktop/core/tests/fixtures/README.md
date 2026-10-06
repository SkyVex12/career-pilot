# Test audio

16 kHz mono 16-bit WAV, synthesized with Windows' built-in text to speech (System.Speech),
so there is no recording of a real person here.

- `them.wav` (voice "Microsoft David"): 0.8 s of silence, "Tell me about a time you scaled
  a system.", 1.5 s, "How would you shard the ledger table?", 1.2 s.
- `me.wav` (voice "Microsoft Zira"): 0.6 s, "Sure. At Acme I moved the ledger to a new
  store.", 0.8 s.

`tests/pipeline.rs` and `tests/engine.rs` mix them with a simulated speaker-to-mic echo to
check that echo cancellation keeps the far end off the "me" channel.
