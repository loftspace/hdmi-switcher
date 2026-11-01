# Windows branch validation

Verified on 2026-10-29 against the local LG C5, before merging.

## Passed

- 63 automated tests: debounce, startup baseline, sleep/wake freshness,
  reconnect, stale cancellation, bounded retry, stop, instance locking,
  configuration, task/plist ownership and local WebSocket protocol tests.
- Ruff lint and formatting; strict mypy on application source.
- Source distribution and wheel build.
- Native Windows x64 PyInstaller packaging, including HID extension.
- Frozen version, device enumeration, real TV input-list query and
  start/status/stop commands.
- Existing pairing key validated without a fresh confirmation prompt.
- TV identifies HDMI 1 as the PC's RTX 5090. User confirms HDMI 2 is the Mac.
- Real HDMI 2 selection and HDMI 1 restoration both confirmed through LG's
  foreground-app API. Mac had no video signal while idle.
- Windows USB topology places Keychron K2 HE and a Razer mouse endpoint behind
  the USB switch hub. The keyboard's vendor/product identity is configured.
- Native branch executables installed in a permanent user folder, and the
  owned per-user login task points at the background executable.
- Background helper started successfully and reports its active instance lock.
- User physically switched PC -> Mac -> PC successfully. The running helper's
  log records USB release followed by HDMI 2, then USB acquisition followed by
  HDMI 1. No program on the Mac was needed for that round trip.

## Not yet passed

- Actual Mac installation, HID enumeration permissions, sleep/wake and five
  physical switches in both directions require the Mac hardware.
- macOS package execution and user login behavior are not verified by Windows
  tests, even if CI passes.

The installed Windows profile switches to the Mac on stable keyboard removal,
and back to the PC on stable arrival. The helper does not wake the Mac or TV.
If Mac video stays asleep, HDMI 2 can be correctly selected with a blank screen.

The optional Mac helper is not required while the Windows PC is awake and its
helper is running. Mac-side runtime testing is deferred; this does not invalidate
the verified Windows-only round trip.

No TV privacy settings were modified by HDMI Follow.

## 0.1.1 switch-hub refinement

The new upstream-hub signal was separately validated on hardware, then the native
0.1.1 branch build was installed and activated. Keyboard-only unplug/replug no
longer switches the input; pressing the switch selects Mac and then Windows.
User confirmation matches the captured hub and helper events. Login startup is
enabled. See [switch-hub investigation](switch-hub-investigation.md) for timing
measurements and the whole-switch-unplug limitation. The updated suite has 107
passing tests; Ruff and strict mypy pass.

## 0.1.2 native notification optimization

Installed and activated the unsigned 0.1.2 branch binaries on 2026-10-29.
The Windows hub profile now uses native Configuration Manager USB notifications,
native suspend/resume callbacks and a native stop event. There are no idle USB
scans or stop-file checks. The authenticated TV session still sends a small
health ping every five seconds. Portable keyboard mode retains polling.

The local profile stabilization delay was reduced from 200 ms to 50 ms; the
general default remains 200 ms. User physically switched to Mac and back and
confirmed both directions work and feel quicker. TV command acknowledgements
were 55 ms for HDMI 2 and 26 ms for HDMI 1, after stabilization. Button-to-picture
latency was not measured and includes USB notification and HDMI negotiation.
The keyboard-only unplug test passed on 0.1.1; it was not repeated on 0.1.2.
The same exact upstream hub identity is still the signal.

A fresh 10-second idle sample of 0.1.1 used 0.140625 CPU seconds across the worker
and executable wrapper, about 1.4% of one core. A 15-second sample of installed
0.1.2 recorded 0 CPU seconds at Windows process-timer resolution for both
processes. This is below measurement resolution, not proof of literally zero
CPU use. Combined private memory was about 25 MB. These are short local samples,
not a long-term power benchmark.

170 automated tests pass, including native callback filtering/lifetime, no idle
rescans, first switch after long idle, duplicate events, reversal, suspend/resume
fences, stop cancellation and live Windows stop signaling. Ruff lint/format and
strict mypy pass. Native packaging and installed start/status/stop were verified.
Independent review found no remaining actionable issues after callback lifetime
and sampling-race fixes. Mac hardware validation remains deferred.

## 0.1.3 missed hub transition recovery

On 2026-11-01 the user reported one missed return after many successful switches.
The installed helper was running and the configured hub was present, but the
log ended with departure and HDMI 2 acknowledgement, with no matching acquisition
or protocol error. This localizes the miss to ownership detection; the exact
notification/inventory timing was not recorded and cannot be proven afterward.

The 0.1.2 event path had no follow-up read when its first inventory snapshot still
matched the old stable state. A notification delivered before inventory updates,
or an entirely missed callback, could therefore leave ownership stale indefinitely.
Regression tests reproduce both cases. Version 0.1.3 rechecks for half a second
after hub events and reconciles once per second as a fallback. Normal native
events still use the configured 50 ms stabilization. Fallback detection adds at
most roughly one second plus stabilization under normal scheduling, rather than
waiting forever. It does not repeatedly force the TV input while ownership stays
unchanged. A long sampling gap resets the baseline silently for suspend safety.

177 tests pass, including recovery in both directions, bounded idle checks,
silent resume and recovery after a missed power notification. Ruff, strict mypy,
source/wheel and unsigned Windows packaging pass. Version 0.1.3 was installed
and started through the existing enabled login task. Physical validation is
recorded separately below when completed. Small periodic USB checks are now an
intentional reliability tradeoff; the 0.1.2 no-idle-scan claim is historical.
