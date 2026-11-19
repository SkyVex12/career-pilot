# Native background controls: integration and verification

Implementation: `apps/desktop/src-tauri/src/background.rs`.

The module adds optional close-to-tray behavior, the existing Ruby app icon in the Windows tray/macOS menu bar, Open Copilot, Pause call suggestions, and Exit completely. Preferences live in the app config directory in `background.json`, independently of existing settings. No credentials are stored there. Defaults preserve foreground startup and ordinary window-close behavior.

## Integration contract

- Register `mod background;` in the Tauri library.
- During setup call `background::setup(app.handle(), remote::end)?`.
- Before existing window event handling call `background::on_window_event(window, event)` and return when it consumes the close.
- The existing single-instance callback calls `background::second_instance(app, &args)`. A second `--background` launch never restores/focuses the workspace; a normal second launch does.
- Register `background_status`, `background_set_preferences`, and optional `background_native_activity` commands.
- Skip startup backend wake/auth fetching when `background::background_start_requested()` is true. The frontend must obtain `background_status` before its startup API/device discovery work and remain idle while `background_start` is true. Listen for `copilot://background`; Open Copilot clears the flag and emits this event. Recheck status after installing the event listener to avoid a lost startup event.
- Background preference changes emit the same event. Future detection adapters must honor `suggestions_paused` before presenting suggestions.
- The native module itself has no backend/API, audio capture, transcription, screen capture, or polling loop. The surrounding app must retain that invariant for hidden startup.

`background_set_preferences` takes `{ preferences: { launch_at_login, keep_running, suggestions_paused } }`. The returned status distinguishes configured preferences from whether the OS startup entry exists. OS policy or user startup controls may still prevent launch. Corrupt preference files are logged and startup uses opt-in defaults.

## Login startup

Windows uses the current user's `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `RubyCopilot`, with a quoted executable path and `--background`. No elevation or shell is needed; the registry command suppresses its console window. Disabling removes only this named value.

macOS writes `~/Library/LaunchAgents/com.copilot.desktop.login.plist`, with XML-escaped executable path and `--background` in a separate `ProgramArguments` element. This uses the long-established LaunchAgent mechanism, without post-Ventura APIs or architecture-specific binaries. There is no KeepAlive restart loop. Enabling/disabling takes effect at the next login; changes do not spawn the app immediately. OS login-item policy and installed app location still matter. macOS behavior requires real-machine validation; it has not been compiled or exercised on this Windows host.

The login entry uses the executable's current location. Re-enable launch at login after moving the application or changing install location.

## Native activity inspection and limitations

`background_native_activity` is a user-requested Windows diagnostic, not a detection promise. It inspects WASAPI session-control metadata across active capture/render devices on a worker thread. It never creates `IAudioClient`, `IAudioCaptureClient`, or an audio stream. Results include active input/output flags by external process ID, optional foreground process ID, and device inspection warnings. Ruby's own process ID and system PID zero are excluded. Additional child/browser processes are not claimed to be fully attributed.

Audio activity and application identity cannot establish that a meeting is in progress. The response therefore always has `call_evidence: false`, and background status has `call_detection_available: false`. No application has verified incoming-call detection, active-call detection, or usable-call-audio support from this component alone. macOS returns an explicit unsupported diagnostic response. Native audio-session event subscriptions and app-specific reliable call evidence remain integration work; this component introduces no polling to substitute for them.

## Validation

Run `cargo test -p copilot-desktop --lib background:: --offline` for preference persistence/corruption handling, opt-in defaults, close/exit policy, background argument recognition, startup path quoting, plist escaping, and own-process exclusion.

Run the Windows-host smoke test explicitly with:

`cargo test -p copilot-desktop --lib background::native_tests::windows_audio_session_inspection_does_not_claim_calls --offline -- --ignored --nocapture`

That smoke test verifies actual read-only WASAPI enumeration on the available host. It does not verify any conferencing app, incoming call, notifications, permission prompts, login/logout behavior, tray interactions, or macOS. Those require real desktop/platform testing. Unit tests do not change login registration or start audio capture.
