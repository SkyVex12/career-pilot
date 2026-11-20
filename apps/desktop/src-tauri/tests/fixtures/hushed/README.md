These sanitized fixtures were captured on 2026-11-20 from Android 14 in the isolated LDPlayer `CareerPilotValidation` instance 1, serial `emulator-5556`.

The notification source was a separate fictional app, `app.careerpilot.callfixture`. It used Android's native `Notification.CallStyle` incoming, ongoing and screening constructors, `MessagingStyle`, `BigTextStyle` with `CATEGORY_MISSED_CALL`, and a call group summary. No audio, network, caller data or Hushed account was used. Hushed was not installed in that validation instance. Only notification framing, flags and typed template/call-type metadata remain; titles, text, person values, actions and intents were discarded. The transient record identity and fixture UID were normalized.

`android14-production-filter.txt` was obtained while the fictional incoming notification existed, using the actual production command's `--package com.hushed.release`. It contains no notification records, proving that the separate fixture is excluded. Unit tests first assert that every unmodified fixture is rejected by the production package check, then normalize the package **inside tests only** to exercise native metadata parsing.

This is framework-format and package-isolation evidence. It does not establish that the real Hushed app emits these signals or verify any actual Hushed call. The initial fixture exposed Android's requirement that CallStyle notifications have a foreground service or full-screen intent; the fixture subsequently used a no-op full-screen intent and all six notification states plus cancellation were captured successfully.

Raw notification dumps were not retained. The fixture APK and build/collection scripts are local ignored validation artifacts in `.implementation/hushed-call-fixture/`.
