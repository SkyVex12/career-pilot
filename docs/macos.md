# Copilot Desktop for macOS

The Mac app targets **macOS 13 Ventura and newer**. One **Universal** app contains native
Apple Silicon (`arm64`) and Intel (`x86_64`) executables; no architecture choice or Rosetta
is needed. macOS 12 and earlier are outside this support range.

Microphone capture and the audio-check tone use native Core Audio. ScreenCaptureKit captures
system audio on macOS 13+, without a virtual audio driver. This is the system/app audio mix,
not a filter for one physical output device. The selected output remains the destination
for the test tone and guest voice (older WebKit also requires it to be the system default).
Credentials use Keychain; `CommandOrControl` shortcuts
use Command on Mac.

## Build on a Mac

Install Xcode Command Line Tools (`xcode-select --install`), Rust stable, Node 22+ and the
pnpm version in the root `package.json`. From the repository root:

```sh
pnpm install --frozen-lockfile
pnpm --filter @copilot/desktop tauri dev
```

To build a Universal preview using the backend in `apps/desktop/release.json`:

```sh
rustup target add aarch64-apple-darwin x86_64-apple-darwin
cd apps/desktop
node scripts/release.mjs config --version 0.0.0-ci --platform darwin-universal --preview
MACOSX_DEPLOYMENT_TARGET=13.0 APPLE_SIGNING_IDENTITY=- pnpm tauri build \
  --target universal-apple-darwin --features release --config src-tauri/tauri.release.conf.json
```

The DMG appears in `target/universal-apple-darwin/release/bundle/dmg/` at the repository root.
Open it and drag Copilot Desktop into Applications, then launch from Applications. Preview
bundles are ad-hoc signed, not notarized releases. If Gatekeeper rejects a downloaded
preview, use a local build; do not disable system security. Previews embed the backend
settings but do not enable automatic updates.

Tauri automatically merges `src-tauri/tauri.macos.conf.json` on Mac. The bundle, Rust build,
and Objective-C bridges all target macOS 13.0. Our bridges treat unguarded newer API use as
a compilation error. Bundle verification checks both executable architectures and their
minimum OS versions before publication. These checks do not replace tests on older Macs.

## Permissions and feature checks

Grant Microphone and Screen Recording when starting the first call. ScreenCaptureKit uses
Screen Recording permission for system audio on Ventura and Sonoma, even when no video is
being shared. If denied, change Copilot Desktop under System Settings > Privacy & Security,
then relaunch. Use the installed app for permission testing; a development process launched
from Terminal can have different permissions.

Before claiming runtime acceptance, test the Universal app on macOS 13, macOS 14.1.2,
and a current macOS release, covering both Apple Silicon and Intel:

- Sign in, quit, reopen, and confirm Keychain restores the session.
- Run Audio Check; confirm microphone/system-audio meters and transcription of both speakers.
- Deny and regrant permissions; verify errors, recovery, and stopping capture.
- Change or disconnect microphones and headphones during a call; run the test tone again.
- Take a screenshot and share a selected window or display, including Retina/external displays.
- Check transparency, click-through escape, shortcuts, sleep/wake, and guest voice routing.
- Test a signed/notarized DMG on another Mac and update between signed releases.

Screen sharing uses ScreenCaptureKit and excludes Copilot from display shares. Window
shares and screenshots capture only the selected window; they never fall back to the whole
display. Screenshots use `SCScreenshotManager` on macOS 14+ and a single complete `SCStream`
frame on Ventura. Screen sharing requests up to 60 fps; actual performance depends on JPEG
encoding, IPC, WebKit and the network.

Guest voice uses WebKit speaker selection when available. On older WebKit versions without
`setSinkId`, the selected call output must also be the Mac's current system output. The app
checks the native default before playback and stops guest playback if it detects a route
change. Older WebKit cannot pin that device; verify headset switching on hardware. Set the
Mac sound output to the call headphones before enabling guest voice. Safari/WebKit 18.4
introduced speaker selection; it is feature-detected rather than used as an OS requirement.

Guest remote control uses Quartz events and requires Accessibility permission. The host must
share a display and authorize the guest; window shares are view-only. Display coordinates use
CoreGraphics points so Retina scaling does not shift clicks. Ending control releases held
keys/buttons; permission loss and display changes end control. Test dragging, multi-clicks,
scrolling, Command/Control/Option shortcuts, and the stop hotkey on hardware.

## Experimental meeting-app capture exclusion

Stealth mode applies an `AVSampleBufferDisplayLayer.preventsCapture` mask to the native
window's frame view, including WebKit content and title bar. It clears the background and
shadow while enabled and restores them and the previous mask when disabled. Legacy window
protection and Dock/app-switcher behavior remain enabled. Ventura uses the display layer's
original rendering API; macOS 14+ uses `sampleBufferRenderer`.

The mask follows resize/backing-scale changes. A main-thread watchdog and wake notification
try to recover a renderer that needs flushing. On recovery failure, normal visible rendering
is restored and the native console reports the error. **An enabled switch is not proof of
capture exclusion.** This is an experimental use of a public video API, not an Apple guarantee
for arbitrary app UI. Activity Monitor retains the signed app name.

Test with a second meeting participant watching the received screen share:

1. With Stealth off, share the Mac display and confirm the main window and overlay appear.
2. Enable Stealth. Both windows must remain usable locally while disappearing at the receiver.
   A black rectangle, visible frame, blank local UI, or transient flash counts as failure.
3. Move/resize windows, switch Spaces, move between displays, hide/show the overlay, and test
   sleep/wake. Disable Stealth and confirm both local rendering and capture visibility return.
4. Repeat for the meeting apps in use. Record OS, app/browser version, and capture mode.

The guest-share feed already excludes Copilot, so it cannot validate this mask. Native CI
compilation and local Windows tests also cannot establish receiver-side compatibility.

If the local interface disappears, quit the app completely and launch it with the mask disabled:

```sh
COPILOT_DISABLE_CAPTURE_MASK=1 /Applications/"Copilot Desktop.app"/Contents/MacOS/copilot-desktop
```

Then disable Stealth in Settings. The override does not reset credentials or other settings.
For development, set the same variable before `pnpm --filter @copilot/desktop tauri dev`.

## CI and public releases

`desktop-macos.yml` runs manually or from the signed-release workflow. Opening a PR or
pushing to `main` does not automatically start a Mac build. One Mac runner installs both Rust
targets, runs native/frontend checks, and creates the Universal app. Main builds update the
shared Rust cache; other branches only restore it.

To build a preview:

```sh
gh workflow run desktop-macos.yml --ref main
```

Use a branch ref to validate changes before merging. The job checks both Mach-O architectures,
minimum macOS versions, and bundle integrity, then uploads a Universal artifact and a
verification receipt. Main builds publish to GCP; other refs produce an artifact only.

To publish an already validated build after merging its exact commit into main:

```sh
gh workflow run desktop-macos.yml --ref main -f source_run_id=RUN_ID
```

This runs only the Linux publisher. The source must be a successful Universal build in this
repository's Mac workflow, its exact commit must now belong to `main`, and its artifact must
be unexpired and match its verification receipt. Old single-architecture artifacts cannot
be republished as Universal. Publication itself runs only on `main`, using the existing GCP
Workload Identity and release service account.

The public download uses the same GCP method as Windows:

`https://storage.googleapis.com/<GCP_RELEASES_BUCKET>/desktop/previews/Copilot-universal.dmg`

The older `Copilot-aarch64.dmg` and `Copilot-x86_64.dmg` preview links remain aliases of the
same Universal app. Immutable build paths retain the source commit and run ID. Preview
publication never replaces the signed release links or `desktop/latest.json`.

Signed releases remain gated by `MACOS_RELEASE_ENABLED=true` and these repository secrets:

- `APPLE_CERTIFICATE`: base64 Developer ID Application certificate (`.p12`).
- `APPLE_CERTIFICATE_PASSWORD` and `APPLE_SIGNING_IDENTITY`.
- `APPLE_ID`, `APPLE_PASSWORD` (app-specific password), and `APPLE_TEAM_ID` for notarization.
- `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` for updater signatures.

The release workflow builds the same version as Windows, publishes `Copilot-universal.dmg`
and compatibility download aliases, and maps both updater keys (`darwin-aarch64` and
`darwin-x86_64`) to the same signed Universal `.app.tar.gz`. The manifest is published last.
Keep the Mac release flag enabled once signed Mac releases are public so future manifests
retain Mac entries. Apple signing/notarization and updater signing are separate requirements.

References: [Tauri Mac bundles](https://v2.tauri.app/distribute/macos-application-bundle/),
[Apple signing/notarization](https://v2.tauri.app/distribute/sign/macos/),
[Tauri updater](https://v2.tauri.app/plugin/updater/),
[Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit),
[WebKit 18.4 speaker selection](https://webkit.org/blog/16574/webkit-features-in-safari-18-4/#webrtc),
[Apple preventsCapture](https://developer.apple.com/documentation/avfoundation/avsamplebufferdisplaylayer/preventscapture).
