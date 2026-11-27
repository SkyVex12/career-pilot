# Earlier hidden route selection

A completed classification can now start the intended ordinary answer request while the answer stays hidden. Previously the classification could wait for the display gate, then cancel the provisional request and start its replacement. This overlaps work with an existing wait; it does not change model generation speed.

The optimization defaults on for OpenAI-only configurations. `COPILOT_EARLY_ROUTE_SELECTION=false` restores display-time route selection. The repository's existing main-branch workflow handles deployment after merge and checks.

## Scope and quality safeguards

- Use the existing positive classifier result only for the current hidden, unconfirmed question with frozen context and preferences.
- Retain early selection only when the existing route policy actually replaces the generation. Unchanged routes keep their original metadata and requests.
- Preserve the classified question label, model policy, prompt, reasoning, frozen evidence, preferences, and output limit.
- Rebuild selected hidden generations on any text correction, including capitalization and punctuation. Retire their prepared matches and buffered output.
- On manual takeover, restore the original provisional architecture and frozen answer plan before applying the forced response policy. Obsolete hidden content cannot leak into Respond, and the original output allowance is retained.
- Defer prepared-answer cases without frozen context to normal release. Preserve display/confirmation gates, cancellation, stale callback, disconnect, and exposed-answer guards.

Revisions can require additional hidden generation and classification work. Earlier availability depends on classification finishing before ordinary release and the text remaining stable. No consistent gain is expected for a request that stays on the same route.

## Isolated PR validation

The feature was extracted onto `origin/main` at `7d0177d`. Unrelated working-tree model-policy, prompt, router, audio, desktop, and infrastructure edits were excluded. Existing test fixtures were minimally updated for the added protocol method and restoration keyword.

[The controlled results](../../evals/answers/results/hidden-route-selection-2026-11-27/pr-validation/report.md) and adjacent `summary.json` describe the isolated feature. The benchmark runs 48 sessions, eight pairs each for technical/light route replacement, technical/deep expert upgrade, and an unchanged-route control. It checks exact intended request/model/parameter/output identity and unchanged measured source hashes. Each arm has the same primary-only preference, normal 300/400 ms gates, eight-word first-show threshold, 80 ms scripted classifier, 400 ms scripted provider first chunk, and a complete punctuated interim 500 ms before synthetic speech end.

The benchmark uses production Session, TurnDetector, AnswerEngine, router, and localhost websocket paths. It verifies no pre-end answer, one public primary, retained cancellation, and no pending work after close. Injected provider timing establishes the scheduling mechanism; it is not a measurement of real model speed or a semantic quality score. The committed summary is compact; the benchmark generates full raw trials when rerun.

```powershell
.venv/Scripts/python.exe evals/bench/hidden_route_selection.py --pairs 8 --output <new-empty-directory>
```

Validation includes all 79 new Session/turn cases, 141 existing compatibility/regression cases, and 593 tests in the broader affected gateway/replay suites (counts overlap). Full isolated Pyright checked 387 files with zero errors/warnings. The PR's required CI checks provide the full stack-backed test run.

## Historical real-provider evidence

The earlier live replay was measured in the original dirty workspace before PR isolation. That workspace also contained unmerged interview-grounding, router, provider, and prompt changes. It is retained as historical evidence for the scheduling mechanism, **not validation of the isolated PR configuration**. Exact source/configuration fingerprints are in [the historical live summary](../../evals/answers/results/hidden-route-selection-2026-11-27/historical-live-summary.json).

| Scenario | Pairs | Baseline median end to first text | Candidate median | Median paired saving |
|---|---:|---:|---:|---:|
| Complete punctuated interim 600 ms before speech end | 2 | 1,109 ms | 742 ms | 367 ms |
| Short interim without early punctuation | 2 | 1,492.5 ms | 1,547 ms | -54.5 ms |

All eight completed primary requests matched their paired model, full-request hash, parameter hash, and output limit. The targeted scenario's savings were 452 and 282 ms. The control moved in opposite directions, showing provider/detector variability rather than a consistent benefit. Two pairs cannot establish production percentiles, real-call opportunity frequency, or quality noninferiority. Dependent detail prompts can vary because they contain the sampled primary answer.

The initial $1 admission budget blocked one session and prevented another from starting; a separate $0.50-capped run completed the remaining control pair. The historical record retains those outcomes and excludes them from latency pairs. Known reported usage was $0.002477 and unresolved reservations were $1.33231; total billed cost is unknown.

## Measurement limits

Neither comparison measures microphone capture, Deepgram recognition, external retrieval, native desktop transport, WebView paint, or live standup quality. The replay harness disables prewarming and follow-user behavior. Request-parity tests protect inputs and generation settings; they do not guarantee identical stochastic answers or replace a blinded semantic quality evaluation.
