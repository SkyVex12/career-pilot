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

Set `EXPO_PUBLIC_API_URL` to the HTTPS API origin and `EXPO_PUBLIC_GATEWAY_URL` to the secure `/v1/companion` WebSocket endpoint in `.env.local`, or enter the addresses in the app. Only these public URLs belong in Expo configuration. Provider keys and account tokens must never be bundled. Sign in with the same existing email account as desktop; select the same profile.

The checked-in `android` and `ios` directories are generated from `app.json`. After changing native configuration, use `npm run prebuild`. On Windows, Expo's CLI skips iOS; `node scripts/prebuild-ios.cjs` runs its platform-neutral template/configuration steps without pretending to run CocoaPods or Xcode. On macOS use `npx expo prebuild --platform ios`, `npx pod-install`, and Xcode 26.4+ or `npm run ios`.

For an Android preview that includes the JS bundle, run `android/gradlew assembleRelease -PreactNativeArchitectures=arm64-v8a` with JDK 17, Android platform `platforms;android-37.0`/build tools 37, NDK 27.1.12297006, and CMake 3.22.1 installed. The generated local release uses the standard development signing key and is for testing only. App Store/Play distribution requires the owner's signing credentials and store accounts. `eas.json` defines internal preview APK and production profiles; no cloud build or publication happens automatically.

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

Sixteen deterministic tests cover HTTPS/WSS/token boundaries, account/profile headers, refresh serialization and signout races, device-specific revocation, replay deduplication, destructive-command confirmation, exact binary PCM framing and lifecycle invalidation. Strict TypeScript and Android/iOS Hermes export pass. Native projects generate successfully. See `BUILD-VALIDATION.md` for the local APK build result.

Physical Android/iOS testing is still required for Bluetooth routing, actual microphone permission prompts, incoming-call interruptions, long-call battery/thermal behavior, airplane-mode transitions and screen-reader/layout behavior on small/large devices. The configured OpenAI/Deepgram backend has separate live-provider validation in the program ledger. Real phone audio routing/quality still needs physical device trials; signed iOS installation needs macOS/device provisioning. Those checks are not represented by bundle success.

Official references: [Expo SDK 58](https://docs.expo.dev/versions/v58.0.0/), [Expo Audio](https://docs.expo.dev/versions/latest/sdk/audio/), [SecureStore](https://docs.expo.dev/versions/latest/sdk/securestore/), [native PCM recorder](https://docs.swmansion.com/react-native-audio-api/docs/inputs/audio-recorder/), [Android commandline tools](https://developer.android.com/studio).

## Optional native CI validation

The `mobile` workflow has explicit manual `native_apk` and `ios_simulator` inputs. The iOS job uses the standard `macos-26` ARM64 runner and installed Xcode26.4, installs CocoaPods, compiles a Release simulator app with signing disabled, and uploads a zipped `.app` plus build evidence for fourteen days. It does not publish to a store or install on a phone. [GitHub's runner catalog](https://github.com/actions/runner-images#available-images) and [macOS26 image manifest](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-arm64-Readme.md) list this runner and Xcode26.4.1; [Expo's SDK table](https://docs.expo.dev/versions/latest/) requires Xcode26.4+ for SDK58. Real iPhone installation still requires provisioning/signing.
