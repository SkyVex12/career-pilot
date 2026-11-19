# Copilot Desktop for macOS

The Mac app uses the shared Tauri/React frontend and Rust call pipeline. Minimum system:

macOS **15.4**, on Apple Silicon or Intel. CPAL 0.18 captures the selected output through a

Core Audio process tap; no virtual audio driver is required. The default keyring backend

stores credentials in Keychain. Global `CommandOrControl` shortcuts use Command on Mac.

## Build on a Mac

Install Xcode Command Line Tools (`xcode-select --install`), Rust stable, Node 22+ and the

pnpm version in the root `package.json`. From the repository root:

```sh

pnpm install --frozen-lockfile

pnpm --filter @copilot/desktop tauri dev

```

For a preview app pointed at the backend in `apps/desktop/release.json`:

```sh

cd apps/desktop

# Intel Macs: use darwin-x86_64 instead.

node scripts/release.mjs config --version 0.0.0-ci --platform darwin-aarch64 --preview

APPLE_SIGNING_IDENTITY=- pnpm tauri build --features release --config src-tauri/tauri.release.conf.json

```

The DMG appears in `target/release/bundle/dmg/` at the repository root. Open it, drag

Copilot Desktop into Applications, then launch from Applications. Preview bundles are

ad-hoc signed for testing, not notarized distribution builds. Build locally if Gatekeeper

rejects a downloaded preview; do not disable system security. Preview builds embed the

backend settings but do not produce signed updater archives.

Tauri automatically merges `src-tauri/tauri.macos.conf.json` on Mac. It declares the minimum

OS, the microphone entitlement, and usage descriptions in `Info.plist`. Use the bundled app

when testing permissions: `tauri dev` runs from a terminal and may have different TCC behavior.

## Permissions and feature checks

Start a session and grant Microphone and system-audio recording when macOS asks. Grant

Screen Recording when taking the first coding screenshot. If denied, change Copilot Desktop

under System Settings > Privacy & Security and relaunch. Screenshot capture selects another

app's window; it does not fall back to a whole display if window capture fails.

Before enabling public releases, test both architectures on a real Mac:

- Sign in, quit, reopen, and confirm Keychain restores the session.

- Run Audio Check; confirm microphone and call-output meters, then transcribe both speakers.

- Deny/regrant permissions and check the error and recovery path.

- Take a coding screenshot, including a Retina display and an external monitor.

- Check overlay transparency, click-through escape, shortcuts, sleep/wake, and device changes.

- Test a signed/notarized DMG on another Mac, then update from one signed version to the next.

Guest remote control uses Quartz events and requires Accessibility permission. The host must
share a display and authorize the guest; window shares are view-only. Display coordinates use
CoreGraphics points so Retina scaling does not shift clicks. Ending control releases held keys
and buttons; permission loss and display changes end control. Test dragging, multi-clicks,
scrolling, Command/Control/Option shortcuts, and the stop hotkey on real hardware.

Screen sharing and screenshots use ScreenCaptureKit through an Objective-C bridge built by
Xcode. The source picker offers displays and other apps' windows. Display shares exclude
Copilot's process, and window shares capture only the selected window. Latest JPEG frames feed
a canvas video track into the existing LiveKit room, with bounded buffering and cleanup on
stop/reload. Capture requests up to 60 fps, but effective throughput depends on JPEG encoding,
IPC, WebKit, and network performance; benchmark this on both architectures before release.
macOS 15.4 is required for WebKit speaker selection (`setSinkId`) used by guest voice.

## Experimental meeting-app capture exclusion

Settings > General > Stealth mode (experimental) now applies an
`AVSampleBufferDisplayLayer.preventsCapture` mask to the native window's frame view,
including its WebKit content and title bar. It clears the window background and shadow
while enabled and restores them and the previous mask when disabled. The existing legacy
window protection and Dock/app-switcher behavior remain enabled alongside it. Hidden
Mac overlays also have their masks removed when the setting is turned off.

The mask follows resize/backing-scale changes. A main-thread watchdog and wake notification
try to recover a video renderer that needs flushing. If recovery fails, normal visible
rendering is restored and a message is written to the native console. **An enabled switch
is not proof of capture exclusion.** This is an experimental use of a public video API,
not an Apple guarantee for arbitrary app UI. Activity Monitor retains the signed app name.

### Test with a meeting receiver

Build/install the preview using the commands above, then:

1. Join a test meeting from a second device/account and watch the received share there.
2. Leave Stealth off, share the entire Mac display, and verify both the main window and
   overlay appear. This is the control that confirms the right display is being observed.
3. Turn Stealth on. Confirm both windows remain readable and interactive on the Mac but
   disappear at the receiver. Check the title bar, shadows, menus and dialogs too; a black
   rectangle, remaining frame, blank local UI, or a transient flash counts as a failure.
4. Move and resize the windows, switch apps and Spaces, move between Retina/external
   displays, hide/show the overlay, and stop/restart sharing. Test sleep/wake separately.
5. Turn Stealth off and verify both normal rendering and capture visibility return,
   including an overlay hidden when you switched the setting off. Repeat several times.
6. Repeat for Zoom desktop, Teams desktop, and Meet in the browser you use. Record macOS
   version, meeting app/browser version, selected capture mode, and full-display versus
   single-window share results. These are separate compatibility results.

Our guest-share feed explicitly filters Copilot out; it cannot validate this experimental
mask. A local capture preview also cannot substitute for the actual meeting receiver.
No receiver-side results or native compilation results are available from this Windows
workspace yet.

### Recover if the local interface disappears

Quit Copilot completely first (a second launch otherwise focuses the existing process).
Launch the installed app from Terminal with the mask disabled:

```sh
COPILOT_DISABLE_CAPTURE_MASK=1 /Applications/"Copilot Desktop.app"/Contents/MacOS/copilot-desktop
```

Then turn Stealth off in Settings. The launch override only disables the experimental
mask; it does not reset credentials or other settings. For development:

```sh
COPILOT_DISABLE_CAPTURE_MASK=1 pnpm --filter @copilot/desktop tauri dev
```

Implementation references: [Apple preventsCapture](https://developer.apple.com/documentation/avfoundation/avsamplebufferdisplaylayer/preventscapture),
[mask experiment and its limitations](https://zenn.dev/d_date/articles/macos-capture-protection-preventscapture?locale=en),
and [Apple's legacy sharing-type guidance](https://developer.apple.com/documentation/appkit/nswindow/sharingtype-swift.enum/none).

Native Mac compilation and hardware acceptance remain required; Windows tests exercise the
shared planner, key mappings, UI stream lifecycle, and release packaging, not Apple frameworks.

## CI and public releases

`desktop-macos.yml` runs frontend, packaging, and Rust library tests, then builds preview
DMGs on native Apple Silicon and Intel runners. It runs for desktop pull requests and
relevant pushes to `main`, and can also be started manually.

After these changes are committed and pushed to GitHub:

1. Open **Actions > desktop-macos > Run workflow**, choose the branch, and run it.
   GitHub must have the workflow on the default branch before showing the manual-run button.
2. Wait for both architecture jobs to finish. Each compiles the Objective-C bridges and
   bundles the app using Xcode on its native architecture.
3. Open the run's **Artifacts** section (or the download link in the job summary):
   `desktop-macos-preview-aarch64` for Apple Silicon, or
   `desktop-macos-preview-x86_64` for Intel.
4. Unzip the artifact and open the DMG. Artifacts are retained for 14 days. Test builds use
   ad-hoc signing and do not need Apple certificates or updater signing secrets. Gatekeeper
   may reject downloaded previews; the local-build instructions above remain available.

This workflow creates downloadable test artifacts; it does not publish preview builds to
installed users. The Windows release workflow calls the same Mac build workflow for signed
public releases when `MACOS_RELEASE_ENABLED=true`.

After native acceptance, configure these repository secrets for public distribution:

- `APPLE_CERTIFICATE`: base64 Developer ID Application certificate (.p12).

- `APPLE_CERTIFICATE_PASSWORD`: its password (may be empty if the certificate has none).

- `APPLE_SIGNING_IDENTITY`: Developer ID Application identity.

- `APPLE_ID`, `APPLE_PASSWORD` (app-specific password), `APPLE_TEAM_ID`: notarization login.

- Existing `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`: updater signing.

Set repository variable `MACOS_RELEASE_ENABLED=true`. The existing desktop release workflow

then calls the Mac workflow with the same commit and version as Windows, requires both Mac

builds to succeed, merges all three updater entries, and publishes the manifest last. Keep

this variable enabled once Mac releases are public, so subsequent manifests retain Mac entries.

Apple signing/notarization and updater signing are distinct; both are required for releases.

Downloads alongside `Copilot-setup.exe`:

- `Copilot-aarch64.dmg` — Apple Silicon.

- `Copilot-x86_64.dmg` — Intel.

The updater consumes versioned `.app.tar.gz` archives, not DMGs. Platform keys are

`darwin-aarch64` and `darwin-x86_64`. `release.mjs manifest --platform ... --bundle-dir ...`

packages one architecture; `release.mjs merge --version X --out latest.json MANIFEST...`

combines the entries and rejects duplicate platforms or mismatched versions.

References: [Tauri macOS bundling](https://v2.tauri.app/distribute/macos-application-bundle/),

[Apple signing and notarization](https://v2.tauri.app/distribute/sign/macos/),

[Tauri updater](https://v2.tauri.app/plugin/updater/).
