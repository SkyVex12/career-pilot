# Controlled hidden-route selection benchmark

Controlled synthetic provider/detector timing through production Session/TurnDetector/AnswerEngine and localhost websocket; not real provider latency, audio/STT latency, paint latency, or a quality judge

All paired intended requests and outputs were identical. All lifecycle and cancellation assertions passed.

Latency is measured from the synthetic speech-end frame to the first client answer delta.

| Scenario | Pairs | Baseline p50 | Candidate p50 | Paired saving p50 |
|---|---:|---:|---:|---:|
| technical-light-to-legacy | 8 | 710.5 ms | 297.0 ms | 406.5 ms |
| technical-deep-to-expert | 8 | 703.0 ms | 297.0 ms | 406.0 ms |
| unchanged-other-light-control | 8 | 312.5 ms | 313.0 ms | 0.0 ms |

Provider first chunk: 400 ms; classifier: 80 ms; full punctuated partial: 500 ms before speech end; normal gates: 300/400 ms. First-show threshold remains 8 words. Both arms use the same explicit primary-only preference.

The unchanged-route case is a negative control. These fixed injected delays establish the overlap mechanism, not real-network savings or model answer quality.
