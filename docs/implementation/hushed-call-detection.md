# Hushed call detection in LDPlayer

Career Pilot has an experimental Windows desktop detector for Hushed call notifications in selected local LDPlayer instances. Incoming-call detection does not require microphone activity. Detection runs locally; it does not require GCP or the mobile companion connection.

This is not yet a verified real-Hushed integration. Hushed explicitly does not support emulators, so behavior can change or fail independently of Career Pilot. See [Hushed's supported-device policy](https://support.hushed.com/hc/en-us/articles/360015505712-Compatible-Devices-What-devices-and-operating-systems-does-Hushed-work-on).

## Set it up

Use a desktop build containing the Hushed settings. For the local Windows preview, exit the existing Career Pilot process using its tray **Exit completely** action before running `.implementation/artifacts/windows/CareerPilot-hushed-detection-preview.exe`; closing the window can leave the older process running. The preview preserves existing settings and is unsigned. Build and test evidence: [Windows preview receipt](evidence/hushed-call-detection-2026-11-20.json).

1. Start the LDPlayer instance containing Hushed and enable **local ADB** in LDPlayer. Identify the intended instance yourself; Career Pilot does not infer an instance from its display name.
2. In Career Pilot desktop settings, open **Background**. Turn on **Suggest assistance for detected calls**, leave **Pause call suggestions** off, and enable **LDPlayer** under **Applications**.
3. Expand **Hushed in LDPlayer**. Check the full **LDPlayer ADB executable** path. Use an existing `adb.exe` from LDPlayer or the Android SDK; a suggested path is only a starting value.
4. Click **Find emulators** and select the intended connected instances, up to four. Enable **Detect Hushed calls**, then click **Save Hushed detection**. Editing the form or finding devices does not save the configuration.
5. Click **Check detection** to read the latest background result. Keep Career Pilot running; enable **Keep running in the background** if you want closing its main window to leave detection available.

If the instance is missing, expand **Connect a local emulator**, enter its explicit `127.0.0.1:PORT` address, and click **Connect local emulator**. Then select it and save. LDPlayer documents `PORT = 5555 + instance index × 2`: index 0 uses `5555`, index 1 uses `5557`. Verify the index in LDPlayer's instance manager or its documented listing commands. See the [official LDPlayer command-line guide](https://jp.ldplayer.net/support/ldconsole-command-line-guide.html).

Only connected local `emulator-N` serials and explicit `127.0.0.1:PORT` endpoints are accepted. Remote devices, `localhost` hostnames, IPv6 endpoints, and duplicate aliases of one endpoint are rejected. Discovery and connection do not start an emulator, change its permissions, or enable ADB for you.

## What the detector recognizes

The adapter checks the exact Hushed package, `com.hushed.release`, and reads redacted Android notification metadata. Supported call evidence requires the framework `android.app.Notification$CallStyle` template and integer `android.callType`: `1` for incoming, `2` for ongoing. Screening calls (`3`), malformed metadata, and unsupported formats do not produce a call prompt. Message, missed-call, voicemail, and group-summary notifications are excluded.

When this mode is enabled, Hushed notifications replace generic LDPlayer microphone/control inference to avoid duplicate prompts. Other applications keep their existing detectors. Global pause, per-application settings, Windows notification suppression, and an already active Career Pilot session still apply. The prompt appears on the Windows desktop.

The detector does not capture audio or save caller details, messages, or raw notification dumps. Detecting a call does not establish that desktop capture can hear the emulator; validate audio routing separately when starting assistance.

The worker waits five seconds between checks. ADB commands have a 1.5-second timeout and bounded output; an instance may require both an installation check and a notification read. Installation checks are cached for up to 30 seconds, and up to four instances are sampled concurrently. This is polling, not a guaranteed instant ring event: brief calls or gaps between consecutive calls can occur between samples and be missed.

## Reading the result

**Check detection** reads cached results rather than launching another scan. Expand **Hushed in LDPlayer** for its separate status and selected/connected counts.

| Status | Meaning and next step |
| --- | --- |
| Incoming / active | Supported call metadata was observed. A prompt still depends on suggestion and Windows notification settings. |
| Idle | No supported call is currently observed; this does not certify compatibility with the installed Hushed version. |
| Not installed | The selected emulator does not report the Hushed package. Check that the correct instance is selected; installation changes may take about 30 seconds plus the next check to appear. |
| Unavailable / configuration error | Check the ADB executable, selected serial, local connection setting, and running instance. |
| Unsupported | The notification format cannot establish call state. Start assistance manually and retain this as an unverified compatibility case. |
| Partial | At least one selected instance works while another is unavailable, lacks Hushed, or has an unsupported format. Check each instance separately. |
| Disabled | Enable the saved Hushed configuration and its global/application suggestion settings. |

## Validate with actual Hushed

Record the Hushed, LDPlayer, Android, and Career Pilot versions, the selected instance, and observed prompt/status behavior. A real Hushed trial is still required:

- Receive a call, answer it, mute/unmute, and end it. Confirm one occurrence stays associated with the call and muting does not create a new prompt.
- Dismiss a prompt, receive consecutive calls, and try a missed call. Confirm dismissals remain dismissed and the next real call can prompt again; include short gaps to expose polling limits.
- Receive SMS and voicemail notifications. They must not create call prompts.
- Disconnect/reconnect local ADB, then repeat with two selected instances. Check unavailable/partial status, recovery, and independent call handling.
- Repeat with suggestions paused, LDPlayer disabled, Windows notifications suppressed, and an active Career Pilot session. Confirm those controls suppress new-call prompts.

Parser/unit tests and synthetic Android framework `CallStyle` fixtures validate the implementation contract. Record their results separately from actual Hushed calls; neither proves that a particular Hushed release exposes the supported metadata.
