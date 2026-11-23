# CoRuby native mobile

Installable Expo SDK 58 / React Native 0.88 application for Android 7+ and iOS 16.4+. Node 22.13+ is required. This is a native companion and standalone client; it is not a responsive browser replacement.

## Run and build

From `apps/mobile`:

```sh
npm ci
npm run typecheck
npm test
npm run export
npm run android
```

Set `EXPO_PUBLIC_API_URL` to the HTTPS API origin and `EXPO_PUBLIC_GATEWAY_URL` to the secure `/v1/companion` WebSocket endpoint in `.env.local`, or enter the addresses in the app. Only these public URLs belong in Expo configuration. Provider keys and account tokens must never be bundled. Sign in to the same account as desktop and select the same profile. Available sign-in methods come from the server. Email codes are shown only when enabled; Career-OS browser sign-in is also supported with an explicit callback-paste step.

The app saves a successfully connected public API/gateway address in device storage and restores it on launch. Account tokens remain separately bound to the API origin in SecureStore. Restoring a connection does not attach a display or start recording. After sign-in, one compatibility request, bounded to four seconds, can show an older-server or unavailable-server notice in **Account and server**. It checks API contracts, not provider credentials or live proxy readiness; individual feature errors remain visible. See [the compatibility contract](../../docs/implementation/client-capabilities.md).

The checked-in `android` and `ios` directories are generated from `app.json`. After changing native configuration, use `npm run prebuild`. On Windows, Expo's CLI skips iOS; `node scripts/prebuild-ios.cjs` runs its platform-neutral template/configuration steps without pretending to run CocoaPods or Xcode. On macOS use `npx expo prebuild --platform ios`, `npx pod-install`, and Xcode 26.4+ or `npm run ios`.

For an Android preview that includes the JS bundle, change into `android`, then run `./gradlew assembleRelease -PreactNativeArchitectures=arm64-v8a` with JDK 17, Android platform `platforms;android-37.0`/build tools 37, NDK 27.1.12297006, and CMake 3.22.1 installed. The generated local release uses the standard development signing key and is for testing only. App Store/Play distribution requires the owner's signing credentials and store accounts. `eas.json` defines internal preview APK and production profiles; no cloud build or publication happens automatically.

## Implemented flows

The four primary tabs are **Call**, **Interview prep**, **Work** and **History**. Call contains desktop companion and in-person modes; Interview prep contains Practice and Meetings; Ask lives in Work. Searchable profile/context selectors replace long button rows. Compact navigation, question/answer cards, source sheets, ruby accents and system dark/light themes adapt to phone and landscape layouts. Opening a selector or source preserves its underlying screen; tab changes preserve in-memory Work state within the same account/profile. Final native layout acceptance is tracked separately below.

- Companion discovers same-account active desktop sessions, explicitly attaches a revocable display, receives snapshots and streamed guidance, adjusts text size, optionally keeps the screen awake, preserves reading position, and reconnects without taking the desktop capture lease. The retained call language is shown when supplied by the server. Answer/follow-up commands distinguish queued from executed; retries retain their command UUID. Ending a desktop session requires explicit confirmation and a summary choice. Disconnecting the display leaves capture running. This flow never requests microphone permission.
- Ask keeps account/profile-scoped conversations in the existing backend, supports all context scopes and date filters, follows up, cancels/retries generation, and opens reauthorized source excerpts without losing the question draft. Original resume text is a distinct authorized source alongside approved facts. Uploaded originals require migration 0027 and bounded worker preparation; legacy preparation delays and unreadable files are reported, not replaced with generated facts labeled as original. Source deletion does not retroactively delete already saved answer excerpts; conversation deletion does.
- Practice reviews selected source context before starting; chooses type, topic, duration, language and application; shows one question; accepts text or microphone transcription; speaks the question on request when configured; and provides pause/skip/retry/hint/sample/save, feedback rubric, evidence, coaching conditions and comparisons. Failed language/application choices have an explicit retry without restarting or discarding the available choices. Current private AI briefs are reauthorized before generation. A saved call transcript line can seed practice from History. Recording/transcription and interviewer playback appear only when the server advertises their respective capabilities; text fallback remains available.
- Preparation reads the same calendar preparation endpoint and matched applications/resumes, displays prior notes/actions, and allows an explicit application correction, saved-call link and unfinished-action edit. Saving an empty action list records completion rather than resurrecting an older series list. Saving context never starts a call. Calendar authorization remains in desktop Settings.
- History pages saved calls, displays outputs, notes and authorized transcripts, imports a source question into practice, and deletes saved calls.
- In-person mode starts a separate native phone-microphone session. The user explicitly chooses the speaker and starts/finishes each spoken turn. PCM16 frames use the existing gateway contract; auth uses a header, never a URL token. This mode does not capture other apps or remote phone-call/system audio. It stops microphone input on backgrounding, unmount, network failure and backpressure. Reconnection requires an explicit tap and never starts the microphone automatically. Leaving the screen ends this phone session without a summary. Desktop discovery excludes mobile capture.

The updated backend supports 96 distinct spoken recognition/text languages through Deepgram; mobile searches the configured server catalog by English name, native name or language code rather than maintaining an unchecked local list. Saved profile language defaults are respected, and unavailable defaults require an explicit supported choice. English, Spanish, French, German, Italian and Japanese also have configured interviewer-audio mappings; other supported languages use text questions with microphone or typed answers. Actual voice controls depend on provider configuration. This does not translate the application interface or certify accents/device quality. An old deployment may still expose only English until its code/configuration is updated; [language setup](../../docs/implementation/call-languages.md) documents the allowlist and capability boundaries.

Connected companion displays can change the spoken language under **Display settings** when the gateway advertises that capability. The selection changes only after the server acknowledges it, and reconnect restores the server's current language. In-person calls can change language between microphone recordings. Older gateways keep live switching disabled; the language can still be chosen before starting a call.

## Privacy and lifecycle

Access/refresh tokens live in platform SecureStore, under keys bound to a hash of the API origin, with device-only unlocked Keychain protection on iOS. Tokens rotate through one serialized refresh. Signout clears local credentials immediately and attempts bounded remote refresh-token/this-phone-grant revocation; failed network revocation is reported. It does not revoke other phones. Profile switches and signout unmount private screens, source drawers, drafts, playback and queued command state. Content is held in memory; there is no plaintext offline database. Background app previews are covered.

A stable random device identifier is stored in SecureStore so an explicit Attach after process recreation can reuse this phone's existing display grant. Reuse requires a fresh authenticated list and an exact current device/profile/session match with an unexpired, unrevoked grant. The identifier is not an account credential and never causes automatic attachment or capture. Failed secure identity storage produces a recoverable error rather than creating throwaway identities. Shared, bounded account state excludes retiring grants across profile remounts; delayed detach/unmount cleanup cannot revoke a newly accepted replacement. A late cancelled attachment response also preserves a grant already adopted by a newer connection.

Practice only uploads the explicit microphone recording, after interviewer playback stops. Temporary raw audio is deleted after reading and on lifecycle cleanup, and the API discards raw audio after transcription. There is a four-minute/eight-MiB recording cap. Generated speech is temporary cache data. A lifecycle generation guard prevents late permission/preparation/playback promises from starting audio after suspension or signout. Native microphone capture is foreground-only; no background audio service is enabled. Android external-storage/overlay permissions and backup are disabled.

Reconnect snapshots replace rendered state and deduplicate sequence numbers. Stale guidance remains visible with a disconnected status; controls require the host to be connected. There is no automatic replay of ambiguous commands or standalone microphone turns.

## Validation and remaining device checks

The final Android package uses source `ed4ef21`, including shared grant retirement across profile changes and retryable practice setup choices, following the malformed-snapshot and terminal WebSocket recovery fixes. **74 deterministic mobile tests** and strict TypeScript pass. Tests cover malformed server data, bounded/cancellable requests, source/draft preservation, practice lifecycle races, capability notices, stable device-grant matching and detach/reconnect races. Final secure Android Release and unsigned iOS simulator builds, all 74 tests, TypeScript and secure exports passed in [mobile run 44690564500](https://github.com/SkyVex12/career-pilot/actions/runs/44690564500), source `d8b9048` with unchanged `ed4ef21` mobile inputs. The [hosted artifact receipt](../../docs/implementation/evidence/mobile-completion-hosted-2026-11-20.json) links the downloads and remote archive digests. [BUILD-VALIDATION.md](BUILD-VALIDATION.md) preserves exact current and historical receipts.

Installable HTTP-test APK: `.implementation/artifacts/mobile/CareerPilot-0.1.0-completion-http-test.apk`, **56,081,446 bytes**, SHA256 `d5c911784be74861a074d4e30ec557b006a48ea7809b2fecb0d8deac84931b7b`. Package/signature checks verify both arm64 and x86_64 audio/SVG/Hermes modules and an embedded bundle requiring no Metro. It uses a development signing certificate. Enter your server address; this build does not embed the old Stage0 IP or local test fixture endpoint.

Scoped native acceptance completed on the separate Android 14 x86_64 **CareerPilotValidation** emulator. The [native receipt](../../docs/implementation/evidence/mobile-native-2026-11-20.json) identifies each tested source/package: the broad `b3736ae` suite covers Work, practice, preparation, history, live companion commands/replay, process restoration, fault recovery and portrait/landscape/large-text/dark layouts; targeted `ed4ef21` checks passed setup 503 → Retry → restored language choices without remounting, and malformed companion snapshot → disabled commands → Retry → live answer and executed command receipt. The final crash buffer was empty. The broad suite restored live answers and enabled commands after a malformed snapshot and observed no crash or ANR. The fictional fixture does not prove deployed server/provider behavior or the cause of the original reported ANR. Physical keyboard/IME occlusion and real phone/audio coverage remain unverified. Use [the isolated acceptance guide](scripts/NATIVE-ACCEPTANCE.md) to reproduce the fixture journeys.

Physical Android/iOS testing is still required for Bluetooth routing, actual microphone permission prompts, incoming-call interruptions, long-call battery/thermal behavior, Wi-Fi/cellular transitions and screen-reader/layout behavior on real small/large devices. The configured OpenAI/Deepgram backend has separate live-provider validation in the program ledger. Real phone audio routing/quality still needs physical device trials; signed iOS installation needs device provisioning. Emulator journeys and bundle success do not establish those results. The final iOS simulator build includes the current UI and native SVG dependency and passed compilation in [run 44690564500](https://github.com/SkyVex12/career-pilot/actions/runs/44690564500). This verifies native compatibility, not physical-iPhone behavior.

Official references: [Expo SDK 58](https://docs.expo.dev/versions/v58.0.0/), [Expo Audio](https://docs.expo.dev/versions/latest/sdk/audio/), [SecureStore](https://docs.expo.dev/versions/latest/sdk/securestore/), [native PCM recorder](https://docs.swmansion.com/react-native-audio-api/docs/inputs/audio-recorder/), [Android commandline tools](https://developer.android.com/studio).

## Optional native CI validation

The `mobile` workflow has explicit manual `native_apk`, `http_test` and `ios_simulator` inputs. With `native_apk=true`, Android builds the secure preview by default. Selecting `http_test=true` as well builds the separate HTTP Test APK described below. `mobile-v*` tag builds always use the secure preview. The iOS job uses the standard `macos-26` ARM64 runner and installed Xcode26.4, installs CocoaPods, compiles a Release simulator app with signing disabled, and uploads a zipped `.app` plus build evidence for fourteen days. It does not publish to a store or install on a phone. [GitHub's runner catalog](https://github.com/actions/runner-images#available-images) and [macOS26 image manifest](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-arm64-Readme.md) list this runner and Xcode26.4.1; [Expo's SDK table](https://docs.expo.dev/versions/latest/) requires Xcode26.4+ for SDK58. Real iPhone installation still requires provisioning/signing.

Before `mobile.yml` exists on the default branch, the existing `desktop-macos` workflow exposes a manual `mobile_ios=true` bridge that calls the branch's mobile workflow. This mode skips desktop build/publication and passes no release credentials.

On Windows, a long checkout path can exceed Ninja's native object-path limit. The optional init script shortens intermediate module names and enables CMake object-path hashing without changing app code or signing. From `android`, append `-I ../scripts/windows-cmake.init.gradle -PmobileCxxDir=C:/path/to/repo/.implementation/cxx` to the preview Gradle command, choosing a short absolute build directory. The local validation used this workaround successfully for the native audio C++ compilation.

## Testing a server without HTTPS (Android)

Use the separate **Career Pilot HTTP Test** APK for an HTTP/WS deployment. It installs as `app.careerpilot.mobile.httptest`, alongside the ordinary app, with its own stored sign-in. The APK embeds its JavaScript and needs no Metro server. HTTP sends sign-in and call content unencrypted; use a test account and fictional content.

To build it in GitHub Actions, manually run the **mobile** workflow on the desired branch with both `native_apk=true` and `http_test=true`. Download the **career-pilot-android-http-test** artifact and extract `app-httpTest.apk`. The APK includes `arm64-v8a` and `x86_64` libraries for Android phones and LDPlayer and uses a development signing key. CI disables dotenv loading and scopes `EXPO_PUBLIC_MOBILE_HTTP_TEST=true` to this build step; enter the current server address in the app. Artifacts are retained for fourteen days. The HTTP option has no effect unless `native_apk` is also selected.

Enter the API origin, such as `http://HOST`, and leave the gateway blank to derive `ws://HOST/v1/companion`. A trailing `/v1` on the API field is accepted and normalized. `/v1/session` is the separate phone-microphone transport and is derived automatically. The Stage0 wake URL is a wake/status service, not an API origin. The VM's public IP may change after a restart.

To reproduce locally after installing the native prerequisites:

```powershell
# Optional public endpoints prefilled in this one build; never put credentials here.
$env:EXPO_PUBLIC_API_URL = 'http://YOUR_TEST_SERVER'
$env:EXPO_PUBLIC_GATEWAY_URL = 'ws://YOUR_TEST_SERVER/v1/companion'
npm run prebuild -- --platform android --no-install
npm run android:http-test -- '-PreactNativeArchitectures=arm64-v8a,x86_64'
```

The build script scopes `EXPO_PUBLIC_MOBILE_HTTP_TEST=true` to its child Gradle process. The `httpTest` variant embeds the bundle, permits native cleartext traffic, and has a distinct app label/package. The Expo config plugin preserves it across prebuild. Normal release builds explicitly prohibit cleartext and reject the HTTP-test JavaScript flag. The flag and public endpoint values are tracked as Gradle bundle inputs. On Windows, append `-I ../scripts/windows-cmake.init.gradle '-PmobileCxxDir=C:/short/cxx'` when needed for long paths.

For a development client, HTTP/WS is allowed only at `localhost`, `127.0.0.1`, or Android emulator host alias `10.0.2.2`. A physical phone can use an ADB reverse tunnel to reach a local server; ordinary release builds still require HTTPS/WSS. iOS does not gain an HTTP exception from this Android build variant.

### Sign-in on Stage0

The historical APK built on 2026-11-19 prefills API `http://35.184.116.247` and companion `ws://35.184.116.247/v1/companion`. Open the desktop app first if Stage0 is asleep; the mobile client does not wake the VM. If the VM restarts, confirm its new address before connecting. The app remembers a subsequently connected address.

The last read-only Stage0 check on 2026-11-19 found Google and Career-OS enabled, with email codes disabled. This mobile build supports Career-OS and email codes when a deployment enables them. Native Google sign-in is not configured. If desktop uses Google, first add Career-OS to that same account in desktop **Settings → Account → Sign-in → Add Career-OS sign-in**; signing into a separate account will not show the existing desktop sessions.

Server connection/sign-in does not prove companion transport works. The deployed public proxy was observed returning 404 for `/v1/companion`; the corrected route passes a local nginx forwarding check but has not been deployed by this work. Updating the APK alone cannot apply that proxy fix, migration 0027, updated API/worker behavior, language configuration or production TLS. The account compatibility notice and individual feature errors identify unavailable/older-server behavior without treating it as successful empty data.

Tap **Sign in with Career-OS**. Approve on the provider's HTTPS site. This testing fallback uses the existing registered desktop callback: the browser ends on a cannot-connect page at `http://127.0.0.1:53683/callback?...`. Copy that entire address back into the app and tap **Finish Career-OS sign-in**. The app checks the callback, state, five-minute expiry and PKCE proof, then consumes the code once. Do not share the callback. An interrupted/expired attempt can be restarted. Account-linking choices return clear desktop instructions instead of silently creating another account.

The test APK skips Java code shrinking for a smaller build-memory footprint; normal release optimization is unchanged. The test app has distinct deep-link schemes as well as package/storage identity. Normal Android native bundles force the secure JavaScript policy after dotenv loading; test bundles explicitly force the test policy. The server's shared `/auth/token/refresh` and `/auth/token/signout` endpoints work even when email sign-in is disabled. Failed remote revocation is reported while local credentials are cleared.

If Windows reports long dependency-header paths despite object hashing, use Ninja 1.12 or newer in the local CMake toolchain. [Ninja 1.12 added Windows long-path support](https://github.com/ninja-build/ninja/releases/tag/v1.12.0); this build uses the installed, Microsoft-signed Visual Studio Ninja 1.13.2 in the workspace SDK, with the original binary preserved. No global SDK or registry change is needed.
