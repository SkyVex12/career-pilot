# Isolated native acceptance

This fixture contains only fictional data and never proxies another server. It listens on host loopback only. It simulates companion, Ask, text practice, saved history, and meeting preparation contracts. It never calls a model, calendar, or microphone provider.

Use a separate development emulator. Current prepared LDPlayer instance: **CareerPilotValidation**, index **1**, serial **emulator-5556**, Android 14. Native ABI is **x86_64** (ARM64 translation is also available). Prefer an APK with x86_64 native libraries. Do not use, copy, clear, or restart the user's instance 0. Recheck the instance name and serial before installing or collecting logs; serial numbers can change.

From the repository root:

```powershell
.venv/Scripts/python.exe -m pytest apps/mobile/tests/test_native_fixture.py -q
.venv/Scripts/python.exe apps/mobile/scripts/native_fixture.py --port 8765
# In another terminal, using the chosen adb executable:
adb -s emulator-5556 reverse tcp:8765 tcp:8765
adb -s emulator-5556 install -r <reviewed-http-test-apk-path>
```

In the HTTP Test app enter API **http://127.0.0.1:8765** and gateway **ws://127.0.0.1:8765/v1/companion**. Sign in as **tester@example.test**, code **123456**. Select **Fictional test profile**. These credentials work only in this local fixture. Secure production builds deliberately reject this plaintext fixture.

The fixture starts in `normal` mode. Change a scenario using a host-only request, then disconnect/reconnect the display as appropriate:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/__fixture -Method Post -ContentType application/json -Body '{"mode":"drop_once"}'
Invoke-RestMethod http://127.0.0.1:8765/__fixture
```

Acceptance assertions:

- `normal`: connect to Fictional product interview. Both “Tell me about a fictional launch.” and “What did you learn?” appear with their answers despite deliberately unordered folded snapshot sequences. Answer/follow-up produces “A fictional live answer arrived successfully.” Reconnect restores generated answers. End requires confirmation and removes the fictional session from discovery. A repeated command UUID must not create duplicate answers (`command_count` counts unique commands).
- `drop_once`: first socket closes after one second; app reconnects, returns to connected state, and restores both answers without duplicates. `joins` reaches 2.
- `slow_attach`: response waits 25 seconds. Connect is cancellable; it must leave busy state within its 15-second deadline. A late successful response must be revoked, not silently attach the screen.
- `reject_attach`: HTTP 503 surfaces a useful retryable error without a crash or indefinite spinner.
- `missing_gateway`: websocket upgrade is rejected; reconnect attempts are bounded and an explicit retry/disconnect remains usable.
- `silent_gateway`: websocket connects but no ready frame arrives; handshake times out at 15 seconds and bounded retry occurs.
- `malformed_snapshot`: invalid nested text raises a visible event error rather than a React render crash. Restore `normal` and retry to recover.
- Background/foreground: display reconnects without starting phone recording. Navigate away during connection and confirm no late attachment reopens the screen. History, Work, and Prep remain navigable. Unsupported fixture operations intentionally return 404 rather than claim fake success.

For this isolated device only, capture `adb -s emulator-5556 logcat -d -b crash` and narrow `AndroidRuntime`/`ActivityManager` logs after a failure. Record package version, ABI, scenario, elapsed time, and precise symptom. Do not infer an ANR cause from an absent trace or from ARM translation availability. Avoid dumping tokens, shared preferences, or any user's emulator. Stop only this helper/fixture after validation, and remove the port forward with `adb -s emulator-5556 reverse --remove tcp:8765` when finished.


Fictional complete journeys (available after restarting the updated fixture):

- **Work / Ask:** send a question. The first detail fetch stays pending; the second returns a clearly labeled fixed fixture answer and source S1. Open its timestamped transcript source, close it, and preserve the unsent draft. Rename/delete conversation and cancel/retry a pending response. General scope returns no private source. Duplicate request IDs do not add another message.
- **History:** open Fictional launch retrospective, read its summary/notes and speaker timestamps. Practice this question imports the exact selected transcript segment. Confirming deletion removes the saved call and makes its old citation return 404.
- **Practice:** review Fictional Acme context, choose a topic/duration, and start text practice. First fetch stays preparing; second presents one question. Submit an answer, poll twice, and inspect versioned fixed feedback with an exact answer excerpt. Try hint/sample, save for later, skip, pause/resume, repeat a question, and finish. The fixture uses the real practice state reducer and rubric validator with deterministic fictional results. Voice and speech capability flags are false; this is not provider certification.
- **Preparation:** open Fictional Acme interview. Review the application source, choose or clear the application and linked saved call, edit unfinished actions, and save. Refresh confirms all selections, including an empty action list. No call is started by saving context.

State is local to this process. Restarting resets these fictional journeys; changing `/__fixture` modes changes companion behavior only. Coordinate a restart before a native run so an in-progress acceptance journey is not lost.
