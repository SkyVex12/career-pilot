# Mobile UI acceptance — 2026-11-22

The Android preview was built from mobile source in `5767e6b419f07747914bdd9d70b87c1e9f77a75a`; a post-build check confirmed no mobile source differences. All **77 mobile tests**, **11 fixture tests**, TypeScript, and whitespace checks passed.

Artifact: `.implementation/artifacts/android/CareerPilot-production-ui-preview.apk` — **56,087,530 bytes**, SHA-256 **65067c17d821ff21e45abb4d46eebd8bdbc4690ca012f3ee6373c616ade1d776**. Package `app.careerpilot.mobile.httptest`, version `0.1.0-http-test`, minimum SDK24, target SDK36, `arm64-v8a` and `x86_64`. APK Signature Scheme v2 verified with the Android Debug certificate. This is an HTTP-enabled internal preview, not a store release. Full source hashes and validation details are in [receipt.json](receipt.json).

Only isolated LDPlayer **CareerPilotValidation**, instance1, serial `emulator-5556`, Android14 / V2242A was used. The user's instance0 was untouched. All app content and credentials came from the local fictional fixture. The device used density320 and size overrides for **390×844 dp portrait**, **844×390 dp landscape**, and **960×600 dp tablet**.

| Evidence | Visible state |
| --- | --- |
| [Onboarding light](onboarding-portrait-light.png), [dark](onboarding-portrait-dark.png) | Workspace/sign-in steps and advanced settings disclosure |
| [Call discovery](call-discovery-portrait-dark.png) | The primary Connect action fits the portrait viewport |
| [Live answer](live-answer-portrait-dark.png) | Connected guidance and successful fictional Answer action |
| [Work answer](work-answer-portrait-dark.png) | Saved Ask answer and source restored after app recreation |
| [History detail](history-detail-portrait-dark.png) | Readable summary, notes, and saved transcript |
| [Practice setup](practice-setup-portrait-dark.png), [languages](practice-languages-portrait-dark.png) | Setup, selectable language catalog, and reviewed fictional context |
| [Practice feedback](practice-feedback-portrait-light.png) | Submitted answer's rubric, evidence, and suggested wording |
| [Landscape live call](live-answer-landscape-light.png) | Compact navigation and reachable live controls |
| [Tablet History](history-tablet-dark.png), [tablet Work](work-answer-tablet-dark.png) | Wide navigation, content, and composer |
| [Changed workspace](changed-workspace-automatic-gateway.png), [connected result](changed-workspace-live-guidance.png) | Final APK: changing saved `127.0.0.1` to `localhost` leaves the gateway Automatic, then signs in and connects successfully |

The layout captures were taken immediately before the final gateway-state fix; their screen components did not change afterward. The two `changed-workspace-*` captures use the delivered APK. Filenames reflect the actual rendered theme. Practice feedback is an expanded completed answer, not the next question. The uncurated intermediate captures under `.implementation/production-ui-mobile` are not final evidence.

No crash or ANR was observed. The isolated crash buffer was empty, and app exit records showed only package updates or user-requested exits. This is bounded device evidence, not a guarantee across devices. Input focus, text entry, and keyboard-aware navigation were exercised, but LDPlayer's IME did not display a standard Android soft keyboard; physical-device keyboard geometry remains unverified. No iOS or physical Android device was used. Speech, model, calendar, production-network, and microphone behavior were outside this fictional UI run.

The fixture and instance1 were stopped after acceptance, and the fixture's ADB reverse mapping was removed. Previous artifacts were preserved.
