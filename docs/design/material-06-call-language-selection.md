# Material 06: Selectable call languages

Status: Agreed feature saved for future implementation. The implementation details below are proposed requirements; saving this material does not start implementation.

Purpose: Let users select one supported spoken language for each call. Speech recognition, question detection, and suggested answers should consistently use that selection.

The user explicitly prioritized selecting a call language over simultaneous multilingual assistance. Bilingual explanations, continuous translation, and independent explanation/answer languages are outside this material's initial scope. Application interface localization is a separate concern.

## Implementation prompt

```text
Implement selectable call languages in Career Pilot.

Repository: C:\Users\A\WorkSpace\career-pilot

Inspect repository instructions and the current settings, call-start protocol, speech-recognition adapters, question detector, retrieval, prompts, post-call outputs, and tests first. Preserve existing user changes. Make the smallest coherent end-to-end change; a dropdown alone does not complete this feature.

1. Selection and defaults

- Add one compact Language selector under Customize on the call setup screen, keeping the main toolbar compact.
- Save a default per profile and allow an override before starting a call.
- Keep English as the default for existing profiles that do not have a selection.
- Snapshot the effective language when a call starts. Changing a profile default must not silently change an active session.
- For the first release, choose the language before starting rather than switching recognition streams midway through a call. Make this behavior clear in the settings.
- Switching profiles must load the correct profile's preference and avoid leaking another profile's draft selection.
- Use clear language names, including a locale or regional distinction only when that distinction actually changes supported behavior.
- Preserve current compact window modes, the start-call flow, and overlay behavior.

One selection controls the call experience. For example, selecting Spanish means recognizing a Spanish conversation and suggesting answers in Spanish. It does not require changing the app's interface language.

2. Supported language catalog

- Establish an explicit, backend-validated list of supported call languages.
- Verify the configured production speech-recognition model's current language support from official documentation before choosing the initial list.
- Verify the complete application behavior for each offered language, including question detection, generation, retrieval, and post-call output.
- Do not claim support solely because the underlying language model can produce text in that language.
- Map stable language identifiers to provider-specific settings in ordinary code. Do not confuse spoken-language selection with existing programming-language controls.
- Validate unsupported or malformed values on the backend and provide an actionable message before starting an unusable call.
- Apply the selection to every configured recognition path and fallback. Do not silently fall back to English when the selected language is unavailable.

The initial non-English language list has not been selected in brainstorming. Keep it a documented product decision and verify a small initial set before widening availability. Build the reusable selection contract without promising support for untested languages.

3. End-to-end propagation

Carry the effective language through the desktop request, protocol, backend session, recognition setup, question detection, answer generation, and retained session metadata.

- Persist it through reconnects, resumed sessions, and supported provider failover.
- Preserve backward compatibility for older clients and settings records using the established English default.
- Keep stored transcripts in their recognized language. Do not translate and overwrite the original transcript.
- Adapt question detection beyond English-only keywords or sentence patterns.
- Generate answers in the selected language while preserving proper names, code, source identifiers, and quoted evidence where needed.
- Keep answer-format choices, such as bullets or paragraphs, independent of spoken language.
- Generate the applicable summaries and feedback in the session language, and retain that metadata for History.
- Do not label old historical content with a guessed language merely because new calls default to English.

Expose the same session-language contract to future practice and mobile integrations so they do not invent incompatible settings. Those features are not prerequisites for delivering selected-language desktop calls.

4. Retrieval and grounding

- Check Unicode handling, tokenization, and language-dependent full-text search behavior.
- Ensure selecting a supported language does not quietly disable useful resume, application, company, or saved-context retrieval.
- Test representative questions with documents in the selected language and with the user's existing documents in another language.
- Reuse existing retrieval capabilities where suitable. Add the smallest necessary improvements, rather than creating a separate knowledge store for every language.
- Keep citations linked to the original source and preserve account/profile boundaries.
- Treat missing evidence as missing evidence; language conversion must not invent experience, decisions, or achievements.
- Explain any deliberately limited source-language combinations in the verified support matrix.

5. Cost and failure behavior

- Use the existing recognition and answer-generation path with the selected language configuration.
- Avoid adding a separate continual translation service or an extra model request solely to route language settings.
- Check provider/model compatibility and any material cost differences before enabling a language in production.
- Keep brief silence, recognition reconnects, and device changes from resetting the selected language.
- Report an unavailable recognition language honestly and preserve the user's selection for the next attempt.

6. Validation and delivery

Use deterministic fixtures for routine tests. Validate settings persistence, profile switching, older-client defaults, rejected identifiers, session propagation, reconnect/failover behavior, prompt language, question detection, citation preservation, and post-call language.

For every offered language, test recorded or live speech with representative questions, technical terms, names, and natural pauses. Assess transcription usefulness, missed questions, answer language, grounding, and response latency together. Do not treat an accent as an error or claim a language is verified from a text-only test.

Preserve Windows and macOS 13+ desktop support, including Apple Silicon and Intel. Separate automated checks, native compilation, and actual speech/device testing in the report. Keep expensive native packaging confined to the relevant existing release workflow.

Deliver the implementation, the tested language matrix, meaningful validation results, setup/configuration notes, and any remaining provider or real-device verification needed. Keep subsequent language additions behind the same validation standard.

Resolve routine implementation choices independently. Obtain the intended initial non-English language priorities when needed; do not invent a commitment to support every language.
```

## Relationship to other materials

- Material 01 starts calls using the selected profile's language preference.
- Material 02 should use the same language identifiers for practice; spoken interviewer output additionally needs a verified speech-generation voice.
- Material 03 may reuse the language catalog, but its conversation preferences must not unexpectedly change an active call.
- Material 04 displays the existing session's selected language and does not create a second recognition pipeline.
- Material 05 can supply evidence in available source languages while the call answer follows the selected language.

See [the six-feature implementation plan](six-feature-implementation-plan.md) for proposed delivery order and milestones.
