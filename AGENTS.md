# HDMI Follow

Small Python background helper for Windows and macOS. USB metadata triggers LG
webOS LAN input selection. Never read HID input reports or capture keystrokes.

## Architecture
- `ownership.py`: pure debounce/generation state machine. Initial state is silent.
- `devices.py`: HID enumeration on the main thread. Never open devices.
- `windows_usb.py`: native present USB instance inventory; failures are unknown,
  never a disconnect. Hub mode never falls back to HID.
- `tv_session.py`: one reusable connection, unique IDs, idle health and cancellation.
- `tv.py`: bounded async SSAP with explicit pairing and stale-ownership guard.
- `runner.py`: portable polling, cancel obsolete work, bound retries.
- `event_runner.py`: Windows hub reactor, native USB/power/stop notifications.
- `windows_notifications.py`, `windows_stop.py`: native callbacks must remain
  rooted until successful synchronous unregister. Keep normal hub switching
  notification-driven; low-frequency reconciliation recovers lost notifications.
- `config.py`, `instance.py`, `autostart.py`, `cli.py`: local lifecycle.
- Secrets and machine-specific configurations stay outside Git.
- Diagnostic logs must never include pairing keys, raw TV frames or exception
  bodies. Log safe error classes and decision reasons; keep periodic logs bounded.
- Do not change TV privacy settings, wake the TV, or infer HDMI mapping silently.
- A command already delivered to the TV cannot be recalled. Never promise otherwise.
- Mac hardware tests require a real Mac. Mark unverified behavior honestly.

## Build, test, release
- Typecheck/lint: `uv run mypy src` and `uv run ruff check .`
- Unit: `uv run pytest`
- E2E (headless): `uv run pytest tests/test_tv.py` (local WebSocket TV); real USB/TV separately. Run when: detector/runner/TV changes; no GUI.
- Build/package: `uv build` and `uv run python scripts/build.py`; unsigned native ZIPs.
- Known failures to tolerate: none.
- Version source: `pyproject.toml` (keep `__version__` equal). Release: release.yml on push to main.
- Install branch build before recommending merge; confirm `hdmi-follow --version`.
- Install locally after merge: extract native ZIP into persistent user folder; confirm version.
- Deploy: GitHub Releases on merge. Signing: unsigned; no notarization.
- Every change uses issue, feature branch and PR. Never merge without specific user approval.

## Verification
Test startup baseline, bounce, resume, disconnect, auth failure and cancellation.
Test Windows-to-Mac loss mode separately from Mac arrival mode. No credential logs.
Hub mode requires physical switch/keyboard-only unplug tests. An entire switch
unplug cannot be distinguished from switch-button removal by hub presence alone.
