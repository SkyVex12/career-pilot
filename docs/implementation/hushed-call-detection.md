# Hushed call detection in LDPlayer

Career Pilot has an experimental Windows desktop detector for Hushed call state in selected local LDPlayer instances. It checks Android Telecom first, with a structured notification fallback. Incoming-call detection does not require microphone activity. Detection runs locally; it does not require GCP or the mobile companion connection.

This is not yet a verified real-Hushed integration. Hushed explicitly does not support emulators, so behavior can change or fail independently of Career Pilot. See [Hushed's supported-device policy](https://support.hushed.com/hc/en-us/articles/360015505712-Compatible-Devices-What-devices-and-operating-systems-does-Hushed-work-on).

## Set it up

Use a desktop build containing the Telecom detector. The new local Windows preview is named `.implementation/artifacts/windows/CareerPilot-hushed-telecom-preview.exe`; its [build/test receipt](evidence/hushed-telecom-2026-11-20.json) records the source revision and checksum. Exit the existing Career Pilot process using its tray **Exit completely** action before opening the preview; closing the window can leave the older process running. The unsigned preview preserves existing settings. The earlier [notification-only preview receipt](evidence/hushed-call-detection-2026-11-20.json) does not validate the Telecom implementation.

1. Start the LDPlayer instance containing Hushed and enable **local ADB** in LDPlayer. Identify the intended instance yourself; Career Pilot does not infer an instance from its display name.
2. In Career Pilot desktop settings, open **Background**. Turn on **Suggest assistance for detected calls**, leave **Pause call suggestions** off, and enable **LDPlayer** under **Applications**.
3. Expand **Hushed in LDPlayer**. Check the full **LDPlayer ADB executable** path. Use an existing `adb.exe` from LDPlayer or the Android SDK; a suggested path is only a starting value.
4. Click **Find emulators** and select the intended connected instances, up to four. Enable **Detect Hushed calls**, then click **Save Hushed detection**. Editing the form or finding devices does not save the configuration.
5. Click **Check detection** to read the latest background result. Keep Career Pilot running; enable **Keep running in the background** if you want closing its main window to leave detection available.

If the instance is missing, expand **Connect a local emulator**, enter its explicit `127.0.0.1:PORT` address, and click **Connect local emulator**. Then select it and save. LDPlayer documents `PORT = 5555 + instance index × 2`: index 0 uses `5555`, index 1 uses `5557`. Verify the index in LDPlayer's instance manager or its documented listing commands. See the [official LDPlayer command-line guide](https://jp.ldplayer.net/support/ldconsole-command-line-guide.html).

Only connected local `emulator-N` serials and explicit `127.0.0.1:PORT` endpoints are accepted. Remote devices, `localhost` hostnames, IPv6 endpoints, and duplicate aliases of one endpoint are rejected. Discovery and connection do not start an emulator, change its permissions, or enable ADB for you.

## What the detector recognizes

The adapter checks the exact Hushed package, `com.hushed.release`. Its primary source is the current `mCalls` section of Android Telecom. A call must name the exact target account component `com.hushed.release/com.hushed.base.telephony.HushedConnectionService` in `tpac`. Historical calls, caller fields, account labels, and incidental package mentions do not establish call evidence. `RINGING` is incoming; `ACTIVE` and `ON_HOLD` are ongoing. Transitional states preserve an existing identity without creating a ringing prompt. Registration alone does not establish an active call.

Telecom process identity and emulator boot identity are both checked before and after the dump. Each current Telecom call has its own internal lifecycle key, allowing multiple and consecutive calls to remain distinct. Two healthy observations of absence retire an occurrence; unsupported or unavailable samples preserve its lifecycle instead of declaring that it ended. No call is ended automatically.

When a registered Hushed Telecom provider has not been established, the notification fallback can recognize the framework `android.app.Notification$CallStyle` template with integer `android.callType`: `1` for incoming, `2` for ongoing. A tracked Telecom occurrence stays protected through an unknown Telecom sample. Screening notifications (`3`), malformed metadata, and unsupported formats do not produce a call prompt. Message, missed-call, voicemail, and group-summary notifications are excluded.

When this mode is enabled, Hushed call metadata replaces generic LDPlayer microphone/control inference to avoid duplicate prompts. Other applications keep their existing detectors. Global pause, per-application settings, Windows notification suppression, and an already active Career Pilot session still apply. The prompt appears on the Windows desktop.

The detector does not capture audio or save caller details, messages, or raw Telecom/notification dumps. Dumps are bounded and parsed transiently; diagnostics retain state and counts. Detecting a call does not establish that desktop capture can hear the emulator; validate audio routing separately when starting assistance.

The worker waits five seconds between checks. ADB commands have a 1.5-second timeout and bounded output. A Telecom check brackets the dump with boot identity and Binder service process ID reads; a notification fallback uses boot identity, service process ID, Telecom dump, and notification dump. Including the package check, there are at most six commands per instance. Package checks are cached for up to 30 seconds, and up to four instances are sampled concurrently. This is polling, not a guaranteed instant ring event: brief calls or transitions can occur between samples and be missed, and command time adds to the five-second wait.

## Reading the result

**Check detection** reads cached results rather than launching another scan. Expand **Hushed in LDPlayer** for its separate status and selected/connected counts.

| Status | Meaning and next step |
| --- | --- |
| Incoming / active | Supported call metadata was observed. A prompt still depends on suggestion and Windows notification settings. |
| Idle | No supported call is currently observed; this does not certify compatibility with the installed Hushed version. |
| Not installed | The selected emulator does not report the Hushed package. Check that the correct instance is selected; installation changes may take about 30 seconds plus the next check to appear. |
| Unavailable / configuration error | Check the ADB executable, selected serial, local connection setting, and running instance. |
| Unsupported | Current call metadata cannot establish call state. Start assistance manually and retain this as an unverified compatibility case. |
| Partial | At least one selected instance works while another is unavailable, lacks Hushed, or has an unsupported format. Check each instance separately. |
| Disabled | Enable the saved Hushed configuration and its global/application suggestion settings. |

## Evidence and remaining real-call validation

The user tried the notification-only preview with Hushed **5.38.1** on `emulator-5554`. ADB connectivity and settings were correct, but the call was not detected. After the call, the captured notification metadata lacked `CallStyle` and `callType`. The exact Hushed Telecom service was registered; no actual Hushed ringing or active Telecom snapshot was captured. Registration and an idle snapshot do not prove live detection. The diagnostic listener was stopped when the user could no longer test.

The updated production detector reads the real selected emulator as registered and idle (one connected emulator, zero calls, 372 ms). Source-derived synthetic fixtures cover Telecom lifecycle states; separate native fixtures validate registered and unregistered idle framing. The isolated Android fixture could not bind using the standard Telecom service action, so a positive native call lifecycle remains unverified. The earlier Android `CallStyle` notification fixture validates only that fallback path. None of these results establishes successful detection of a real Hushed call by the new Telecom preview.

Record the Hushed, LDPlayer, Android, and Career Pilot versions, the selected instance, and observed prompt/status behavior. A real Hushed trial is still required:

- Receive a call, answer it, mute/unmute, and end it. Confirm one occurrence stays associated with the call and muting does not create a new prompt.
- Dismiss a prompt, receive consecutive calls, and try a missed call. Confirm dismissals remain dismissed and the next real call can prompt again; include short gaps to expose polling limits.
- Receive SMS and voicemail notifications. They must not create call prompts.
- Disconnect/reconnect local ADB, restart the emulator, then repeat with two selected instances and overlapping calls. Check unavailable/partial status, recovery, separate call identities, and fresh identity after restart.
- Repeat with suggestions paused, LDPlayer disabled, Windows notifications suppressed, and an active Career Pilot session. Confirm those controls suppress new-call prompts.

Record parser/unit results, native synthetic fixture results, and actual Hushed call results separately, including failures and missing observations. A complete real-Hushed incoming/answer/end trial remains required before describing this integration as reliable.
