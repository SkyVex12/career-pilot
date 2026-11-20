# Career Pilot native mobile

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

The checked-in `android` and `ios` directories are generated from `app.json`. After changing native configuration, use `npm run prebuild`. On Windows, Expo's CLI skips iOS; `node scripts/prebuild-ios.cjs` runs its platform-neutral template/configuration steps without pretending to run CocoaPods or Xcode. On macOS use `npx expo prebuild --platform ios`, `npx pod-install`, and Xcode 26.4+ or `npm run ios`.

For an Android preview that includes the JS bundle, change into `android`, then run `./gradlew assembleRelease -PreactNativeArchitectures=arm64-v8a` with JDK 17, Android platform `platforms;android-37.0`/build tools 37, NDK 27.1.12297006, and CMake 3.22.1 installed. The generated local release uses the standard development signing key and is for testing only. App Store/Play distribution requires the owner's signing credentials and store accounts. `eas.json` defines internal preview APK and production profiles; no cloud build or publication happens automatically.

## Implemented flows

- Companion discovers same-account active desktop sessions, explicitly attaches a revocable display, receives snapshots and streamed guidance, adjusts text size, optionally keeps the screen awake, preserves reading position, and reconnects without taking the desktop capture lease. Answer/follow-up commands distinguish queued from executed; retries retain their command UUID. Ending a desktop session requires explicit confirmation and a summary choice. Disconnecting the display leaves capture running. This flow never requests microphone permission.
- Ask keeps account/profile-scoped conversations in the existing backend, supports all context scopes and date filters, follows up, cancels/retries generation, and opens reauthorized source excerpts without losing the question draft. Source deletion does not retroactively delete already saved answer excerpts; conversation deletion does.
- Practice reviews selected source context before starting; chooses type, topic, duration, language and application; shows one question; accepts text or microphone transcription; speaks the question on request when configured; and provides pause/skip/retry/hint/sample/save, feedback rubric, evidence, coaching conditions and comparisons. A saved call transcript line can seed practice from History. Text fallback remains available without voice providers.
- Preparation reads the same calendar preparation endpoint and matched applications/resumes, explicitly reviews ambiguous context, and displays prior notes/actions. Calendar authorization and original source correction remain available in desktop Settings/Work/Interview prep.
- History pages saved calls, displays outputs, notes and authorized transcripts, imports a source question into practice, and deletes saved calls.
- In-person mode starts a separate native phone-microphone session. The user explicitly chooses the speaker and starts/finishes each spoken turn. PCM16 frames use the existing gateway contract; auth uses a header, never a URL token. This mode does not capture other apps or remote phone-call/system audio. It stops microphone input on backgrounding, unmount, network failure and backpressure. Reconnection requires an explicit tap and never starts the microphone automatically. Leaving the screen ends this phone session without a summary. Desktop discovery excludes mobile capture.

## Privacy and lifecycle

Access/refresh tokens live in platform SecureStore, under keys bound to a hash of the API origin, with device-only unlocked Keychain protection on iOS. Tokens rotate through one serialized refresh. Signout clears local credentials immediately and attempts bounded remote refresh-token/this-phone-grant revocation; failed network revocation is reported. It does not revoke other phones. Profile switches and signout unmount private screens, source drawers, drafts, playback and queued command state. Content is held in memory; there is no plaintext offline database. Background app previews are covered.

Practice only uploads the explicit microphone recording, after interviewer playback stops. Temporary raw audio is deleted after reading and on lifecycle cleanup, and the API discards raw audio after transcription. There is a four-minute/eight-MiB recording cap. Generated speech is temporary cache data. A lifecycle generation guard prevents late permission/preparation/playback promises from starting audio after suspension or signout. Native microphone capture is foreground-only; no background audio service is enabled. Android external-storage/overlay permissions and backup are disabled.

Reconnect snapshots replace rendered state and deduplicate sequence numbers. Stale guidance remains visible with a disconnected status; controls require the host to be connected. There is no automatic replay of ambiguous commands or standalone microphone turns.

## Validation and remaining device checks

Thirty deterministic tests cover production/development/HTTP-test transport boundaries, native bundle environment isolation, Career-OS PKCE callbacks and shared token refresh/revocation, account/profile headers, refresh serialization and signout races, device-specific revocation, replay deduplication, destructive-command confirmation, exact binary PCM framing and lifecycle invalidation. Strict TypeScript and Android/iOS Hermes export pass. Native projects generate successfully. See `BUILD-VALIDATION.md` for the local APK build result.

Physical Android/iOS testing is still required for Bluetooth routing, actual microphone permission prompts, incoming-call interruptions, long-call battery/thermal behavior, airplane-mode transitions and screen-reader/layout behavior on small/large devices. The configured OpenAI/Deepgram backend has separate live-provider validation in the program ledger. Real phone audio routing/quality still needs physical device trials; signed iOS installation needs macOS/device provisioning. Those checks are not represented by bundle success.

Official references: [Expo SDK 58](https://docs.expo.dev/versions/v58.0.0/), [Expo Audio](https://docs.expo.dev/versions/latest/sdk/audio/), [SecureStore](https://docs.expo.dev/versions/latest/sdk/securestore/), [native PCM recorder](https://docs.swmansion.com/react-native-audio-api/docs/inputs/audio-recorder/), [Android commandline tools](https://developer.android.com/studio).

## Optional native CI validation

The `mobile` workflow has explicit manual `native_apk` and `ios_simulator` inputs. The iOS job uses the standard `macos-26` ARM64 runner and installed Xcode26.4, installs CocoaPods, compiles a Release simulator app with signing disabled, and uploads a zipped `.app` plus build evidence for fourteen days. It does not publish to a store or install on a phone. [GitHub's runner catalog](https://github.com/actions/runner-images#available-images) and [macOS26 image manifest](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-arm64-Readme.md) list this runner and Xcode26.4.1; [Expo's SDK table](https://docs.expo.dev/versions/latest/) requires Xcode26.4+ for SDK58. Real iPhone installation still requires provisioning/signing.

Before `mobile.yml` exists on the default branch, the existing `desktop-macos` workflow exposes a manual `mobile_ios=true` bridge that calls the branch's mobile workflow. This mode skips desktop build/publication and passes no release credentials.

On Windows, a long checkout path can exceed Ninja's native object-path limit. The optional init script shortens intermediate module names and enables CMake object-path hashing without changing app code or signing. From `android`, append `-I ../scripts/windows-cmake.init.gradle -PmobileCxxDir=C:/path/to/repo/.implementation/cxx` to the preview Gradle command, choosing a short absolute build directory. The local validation used this workaround successfully for the native audio C++ compilation.

## Testing a server without HTTPS (Android)

Use the separate **Career Pilot HTTP Test** APK for an HTTP/WS deployment. It installs as `app.careerpilot.mobile.httptest`, alongside the ordinary app, with its own stored sign-in. The APK embeds its JavaScript and needs no Metro server. HTTP sends sign-in and call content unencrypted; use a test account and fictional content.

Enter the API origin, such as `http://HOST`, and leave the gateway blank to derive `ws://HOST/v1/companion`. A trailing `/v1` on the API field is accepted and normalized. `/v1/session` is the separate phone-microphone transport and is derived automatically. The Stage0 wake URL is a wake/status service, not an API origin. The VM's public IP may change after a restart.

To reproduce locally after installing the native prerequisites:

```powershell
# Optional public endpoints prefilled in this one build; never put credentials here.
$env:EXPO_PUBLIC_API_URL = 'http://YOUR_TEST_SERVER'
$env:EXPO_PUBLIC_GATEWAY_URL = 'ws://YOUR_TEST_SERVER/v1/companion'
npm run prebuild -- --platform android --no-install
npm run android:http-test -- -PreactNativeArchitectures=arm64-v8a
```

The build script scopes `EXPO_PUBLIC_MOBILE_HTTP_TEST=true` to its child Gradle process. The `httpTest` variant embeds the bundle, permits native cleartext traffic, and has a distinct app label/package. The Expo config plugin preserves it across prebuild. Normal release builds explicitly prohibit cleartext and reject the HTTP-test JavaScript flag. The flag and public endpoint values are tracked as Gradle bundle inputs. On Windows, append `-I ../scripts/windows-cmake.init.gradle '-PmobileCxxDir=C:/short/cxx'` when needed for long paths.

For a development client, HTTP/WS is allowed only at `localhost`, `127.0.0.1`, or Android emulator host alias `10.0.2.2`. A physical phone can use an ADB reverse tunnel to reach a local server; ordinary release builds still require HTTPS/WSS. iOS does not gain an HTTP exception from this Android build variant.

### Sign-in on the current server

The APK built on 2026-11-19 prefills the current API `http://35.184.116.247` and companion `ws://35.184.116.247/v1/companion`. Open the desktop app first if Stage0 is asleep; the mobile client does not wake the VM. If the VM restarts, confirm its new address before connecting.

The current Stage0 deployment enables Google and Career-OS, with email codes disabled. This mobile build supports Career-OS and email codes when a deployment enables them. Native Google sign-in is not configured. If desktop uses Google, first add Career-OS to that same account in desktop **Settings → Account → Sign-in → Add Career-OS sign-in**; signing into a separate account will not show the existing desktop sessions.

Tap **Sign in with Career-OS**. Approve on the provider's HTTPS site. This testing fallback uses the existing registered desktop callback: the browser ends on a cannot-connect page at `http://127.0.0.1:53683/callback?...`. Copy that entire address back into the app and tap **Finish Career-OS sign-in**. The app checks the callback, state, five-minute expiry and PKCE proof, then consumes the code once. Do not share the callback. An interrupted/expired attempt can be restarted. Account-linking choices return clear desktop instructions instead of silently creating another account.

The test APK skips Java code shrinking for a smaller build-memory footprint; normal release optimization is unchanged. The test app has distinct deep-link schemes as well as package/storage identity. Normal Android native bundles force the secure JavaScript policy after dotenv loading; test bundles explicitly force the test policy. The server's shared `/auth/token/refresh` and `/auth/token/signout` endpoints work even when email sign-in is disabled. Failed remote revocation is reported while local credentials are cleared.

If Windows reports long dependency-header paths despite object hashing, use Ninja 1.12 or newer in the local CMake toolchain. [Ninja 1.12 added Windows long-path support](https://github.com/ninja-build/ninja/releases/tag/v1.12.0); this build uses the installed, Microsoft-signed Visual Studio Ninja 1.13.2 in the workspace SDK, with the original binary preserved. No global SDK or registry change is needed.
