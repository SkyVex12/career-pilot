# Call languages: capabilities and validation

Updated 2026-11-23. Calls and practice share **96 distinct spoken-language identifiers** using the existing Deepgram Nova-3 integration. This changes recognition and generated content; the application interface is not translated.

## Coverage

The catalog follows [Deepgram's monolingual Nova-3 language list](https://developers.deepgram.com/docs/models-languages-overview/), checked on 2026-11-23. Regional and script variants of one language are not counted as extra languages; Cantonese and Mandarin are distinct. The exact IDs, native names, script directions and provider mappings live in [the shared catalog](../../packages/core/src/copilot_core/languages.py). Existing saved IDs remain valid.

The user's later requirement to use only existing Deepgram support supersedes the earlier 100+ target. No additional speech provider, package, cloud API, service account role or deployment secret is introduced. Multilingual auto-detection and older Deepgram model families are outside this change.

All 96 have call transcription and practice microphone routes. Generated answers and practice feedback receive the chosen language instruction. [Aura's voice catalog](https://developers.deepgram.com/docs/tts-models) supplies six of this set: English, Spanish, French, German, Italian and Japanese. The other 90 use text interviewer questions with microphone or typed answers. `voice_available` means microphone transcription; `speech_available` means generated interviewer audio.

## Search and live switching

Desktop and mobile pickers search English names, native names and language codes. Desktop supports keyboard navigation; mobile uses a bounded, virtualized list. Setup defaults remain profile-specific.

An active call can change language without ending the session. The desktop exposes Language in the live call settings and overlay; the phone exposes it in live settings. In-person phone recording must finish its current turn before changing. Pause speaking briefly while a switch is confirmed. Completed transcripts and answers keep their original text; future recognition and generated content use the accepted language.

The shared protocol adds `session.set_language` with a UUID `request_id`, and the authoritative `session.language` response. Rejections retain the current language and provide a correlated error. `session.ready.language_switching` gates clients on older gateways. Phone companions use the existing authorized command relay, with the same confirmation; no second capture pipeline is created. Reconnect and display snapshots restore the accepted language.

The gateway serializes changes, closes the old recognition streams, isolates stale callbacks and language-specific answer caches, and persists accepted changes for post-call generation. Unsupported or disabled selections cannot silently fall back to English. Deployment requires updated API, gateway, worker, desktop and mobile builds; older installed clients do not gain new UI controls automatically.

## Deployment configuration and compatibility

- `COPILOT_DEEPGRAM_MODEL=nova-3` (or `nova-3-general`) and `COPILOT_STT_PROVIDERS=deepgram` enable the 96-language call catalog. The API and gateway use the same shared provider/model policy. A non-English call is rejected before a session claim if any configured/actual recognition fallback cannot honor its selection. English remains backward compatible.
- Unset `COPILOT_CALL_LANGUAGES` enables all 96. Set a comma-separated subset only for an intentional operator restriction. The old `COPILOT_VERIFIED_CALL_LANGUAGES` variable remains a compatibility alias; the new name takes precedence. **Remove a legacy explicit `en` value or set the new complete list when upgrading**. Environment variables in an old deployment are not changed by updating the APK alone.
- API speech needs `DEEPGRAM_API_KEY`. `COPILOT_PRACTICE_TTS_MODELS` overrides the six default Aura models. An old explicit English-only map still limits playback until updated or removed. A missing voice/key is an actionable text fallback. Mismatched language/voice mappings are never advertised or silently routed to an English voice.
- Each selected language uses the same monolingual Nova-3 request path; there is no continual translator. [Provider pricing](https://deepgram.com/pricing) distinguishes model/mode/features and opt-out; confirm the account’s plan for final charges. No language-specific price guarantee is made.

Selection is snapshotted into gateway state and durable session metadata. It survives both microphones, factory retries, reconnects and restored sessions. Profile changes never change an active call. `/companion/sessions` and attach responses expose `spoken_language` from that retained session, or null when unavailable; History never guesses a language for legacy records.

## Runtime and grounding

Unicode-aware length heuristics handle unspaced scripts, Indic combining marks and Arabic punctuation. Local question cues exist for the original 24 languages and selected expanded languages; uncued languages still use the model detector. The model detector remains authoritative; local cues only control speculative work. Language rules reach detector Responses/Decisions paths, fast/deep answers, screen prompts, rolling summaries, post-call output and practice. Fixed post-call headings and consent-free metadata summaries retain the original 24 translations; the additional languages use English fixed labels while generated content receives the selected-language instruction. Original transcripts, source IDs, citation markers, names and code remain intact.

Ask lexical retrieval now preserves combining marks, casefolds text, and handles CJK bigrams with embedded Latin product names. For weak/no lexical evidence, it reuses the configured embedding provider in one transient batch of a category-balanced 32-source window (at most 1,500 content characters per source). Only cosine matches at or above `COPILOT_ASK_SEMANTIC_MIN_SIMILARITY` (default 0.55) supplement lexical hits. Failure does not insert unrelated recent records. Explicit application/document/call scope retains its bounded source fallback. This is bounded recall, not exhaustive multilingual search, and it adds a small embedding request only when needed; no new provider or persistent plaintext index is introduced.

Original resume text is now a resolvable source independently of approved fact extraction, under profile RLS and encryption. Authored text is available directly; uploaded files use bounded worker extraction into an encrypted, content-hash-bound cache. Legacy uploads are prepared lazily and report that delay instead of disguising generated facts as original text. Migration 0027 and the updated worker are required; scanned-only or unsupported documents need usable text. See [original resume preparation](resume-original-text.md) for limits and deployment order. Practice preview includes current account/profile/application private briefs without a provider call; each generated turn reauthorizes original sources and re-reads active brief versions, so deleting or replacing a brief prevents stale evidence from reappearing in later generation. Prior questions/answers remain historical records until practice is deleted.

Desktop selectors render native language names and script direction. A legacy server that returns 404 for the catalog remains usable in English compatibility mode and displays a server-upgrade notice. Mobile reports the server's advertised language contract separately from actual catalog/provider availability; see [mobile/server compatibility](client-capabilities.md).

[Keyterm prompting](https://developers.deepgram.com/docs/keyterm) is available on Nova-3. Practice uploads include at most 20 short product/name hints from the current question and source labels, while microphone bytes alone become the transcript. Streaming/practice hints set `redact_usage=true`; practice audio uses model-improvement opt-out.

## Evidence and limitations

[Expanded-route receipt](evidence/call-language-expansion-2026-11-23.json): Cantonese and Welsh streaming routes returned Results and Metadata for synthetic silence. Welsh timed out on its first handshake and passed one bounded retry. This validates routing only.

The earlier evidence below covers the original 24-language release. Expanded catalog routing and live-switch checks are recorded in the [progress ledger](six-feature-progress.md). Catalog membership is documented provider support, not a claim that every language has received native-speaker or real-device quality testing.

Scoped Ruff and Pyright pass. The broad 349-check regression selection passed 348 checks; its strict timing check observed 125 ms against a 100 ms threshold on the loaded Windows host and passed on the isolated rerun. The final focused language/API/stack run passed 230 tests after capability and voice-hint changes.

The committed 24-language text corpus covers questions, acknowledgments, and technical evidence. Deterministic checks exercise each language’s prompt rules, original-source/citation preservation, post-call labels, both recognition factories, retries, retained-state restore, catalog/model/fallback policy and practice transcription wire mapping. Additional migrated-stack tests cover profile defaults, raw-resume citation isolation/deletion, current private-brief reuse/removal, and the existing Ask/practice/companion/calendar lifecycles.

[Sanitized real-provider receipt](evidence/call-language-provider-2026-11-20.json) records bounded synthetic checks, using existing configured credentials and no user recordings/private records:

- Spanish and Japanese Aura→Nova-3 round trips succeeded. Initial unhinted recognition mangled PostgreSQL/Acme; adding the implemented keyterm hints preserved both names (Japanese lowercased them). Final measured combined TTS/STT durations were 6,063 ms and 4,328 ms. These two samples establish adapter compatibility, not speech accuracy or accent/device certification.
- Two real answer requests returned Spanish/Japanese answers, preserved 200→120 ms evidence, cited `[S1]`, and explicitly acknowledged absent query/index details.
- One 26-text `text-embedding-3-large@1024` batch compared 24 translations with English evidence and an unrelated distractor. 21/24 cleared 0.55. Tamil 0.3483, Telugu 0.5291 and Punjabi 0.5092 did not; the threshold is intentionally retained. Use same-language evidence or explicit application/document/call context when broad cross-language recall is insufficient. This one synthetic fact is not a retrieval quality benchmark.

Representative human speech, multiple accents, physical microphone/headphone/speaker behavior, and end-to-end latency on Windows/macOS/iOS/Android remain device/quality acceptance work. The catalog describes implemented provider capability, not those certificates. Unavailable credentials, a legacy deployment or an unsupported fallback remain explicit failures.

## Reproduce

```powershell
.venv/Scripts/python.exe -m pytest packages/core/tests/test_languages.py apps/gateway/tests/test_call_languages.py apps/gateway/tests/test_live_languages.py apps/api/tests/test_language_capabilities.py apps/api/tests/test_call_languages.py -q -p no:cacheprovider
# Opt-in: at most 4 synthetic speech requests, 1 embedding batch, 2 answer requests.
.venv/Scripts/python.exe scripts/validate_language_capabilities.py --live --output .implementation/language-provider-check.json
```

The live check requires configured providers and external network access. It never starts a server session or writes to production storage. Retrying it incurs fresh provider work; it is not part of routine CI.
