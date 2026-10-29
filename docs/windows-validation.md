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
