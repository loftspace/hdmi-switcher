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

## Not yet passed

- Physical USB switch button and real connection-event timing are awaiting
  user verification. Device topology alone does not prove disconnect behavior.
- Actual Mac installation, HID enumeration permissions, sleep/wake and five
  physical switches in both directions require the Mac hardware.
- macOS package execution and user login behavior are not verified by Windows
  tests, even if CI passes.

The installed Windows profile switches to the Mac on stable keyboard removal,
and back to the PC on stable arrival. The helper does not wake the Mac or TV.
If Mac video stays asleep, HDMI 2 can be correctly selected with a blank screen.

No TV privacy settings were modified by HDMI Follow.
