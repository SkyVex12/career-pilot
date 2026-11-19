# Call languages: implementation and verification

Updated 2026-11-19. Stable identifiers control spoken language independently of programming-language and answer-format settings.

| Identifier | Name | Recognition mapping | Deterministic runtime checks | Production availability |
| --- | --- | --- | --- | --- |
| en | English | Deepgram nova-3, language=en | Passed; older client and retained-state default | Enabled by default |
| es | Spanish | Deepgram nova-3, language=es | Passed; Unicode question cues, prompts, factories, retained state, labels | Candidate; disabled until deployment verification |
| vi | Vietnamese | Deepgram nova-3, language=vi | Passed; Unicode question cues, prompts, factories, retained state, labels | Candidate; disabled until deployment verification |

[Deepgram official model/language catalog](https://developers.deepgram.com/docs/models-languages-overview), checked 2026-11-19, lists Spanish and Vietnamese for nova-3. Provider documentation establishes model compatibility, not whole-application or accent/device certification. No new live or recorded speech benchmark was performed in this implementation.

`COPILOT_VERIFIED_CALL_LANGUAGES=en` is the default release gate. After completing deployment verification, operators may explicitly set a comma-separated list such as `en,es`. Non-English calls require nova-3 and Deepgram on every configured recognition path. Unsupported, malformed and disabled identifiers are rejected before session claims or paid work. This does not add a translation service or a separate language-routing model call.

The start choice is snapshotted, retained in gateway state and session metadata, and reused for both microphones, reconnects, restored sessions and provider factory retries. Profile defaults cannot change active language. Language rules reach detector Responses and Decisions paths, fast/deep answers, screen explanation/code prompts and rolling/post-call summaries. Original transcripts and source/citation identifiers remain intact. Historical rows without metadata remain unknown; the migration does not backfill a guessed language.

Prepared answers lack source-language metadata, so non-English calls generate localized output from the existing pack evidence rather than displaying prepared talking points verbatim. Existing resume/company retrieval and embedding paths remain in use; no parallel knowledge store is created. Cross-language retrieval quality, English full-text-search behavior and source-language coverage require deployment-specific corpus evaluation before enabling candidates.

## Checks

149 tests passed across gateway language, Decisions, question coverage prompts, turns, providers, resume, screenshots and session-flow suites. The final dedicated language suite adds retained-state restore coverage and has 21 passing tests. Scoped Ruff and Pyright checks pass. Run `.venv/Scripts/python.exe -m pytest apps/gateway/tests/test_call_languages.py -q -p no:cacheprovider` for the focused contract checks.

Worker session-log integration was attempted but PostgreSQL at localhost:5432 was unavailable (connection refused/access denied); it is not counted as passing. Run the existing worker session-log suite against the migrated local stack before release.

## Remaining release evidence

For every newly enabled language, record representative speech with technical terms, proper names, natural pauses and multiple accents; assess transcription usefulness, missed questions, answer language, grounding/citations and latency together. Test same-language and cross-language resume/company evidence. Verify fallback/provider availability and current pricing on the deployed model. Verify Windows and macOS 13+ actual microphone/device/reconnect paths independently of Python fixtures and native compilation. Candidate languages remain disabled until these results are recorded; a text-only test does not certify speech support.
