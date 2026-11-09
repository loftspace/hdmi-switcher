# HDMI Follow

**Press your USB switch. Let your LG TV follow.**

[![CI](https://github.com/loftspace/hdmi-switcher/actions/workflows/ci.yml/badge.svg)](https://github.com/loftspace/hdmi-switcher/actions/workflows/ci.yml)

A small background helper for Windows and macOS. On Windows it can observe the
USB switch's own upstream hub, then select an LG webOS HDMI input
over your local network. No extra TV modification, no keystroke capture, no cloud
account, and no admin access for normal use.

## What it does

- Stable switch-hub arrival selects this computer's HDMI input. Keyboard-only
  mode remains available explicitly for switches without a usable hub signal.
- Optional device removal selects the other computer's input. This lets Windows
  handle both directions on its own while the Windows PC is awake. A Mac helper
  is optional, useful when the Windows PC is asleep or switched off.
- Reuses an authenticated TV connection instead of reconnecting on every switch.
- Windows hub mode reacts to native USB notifications, briefly rechecks inventory
  while a device settles, and reconciles once per second to recover missed
  notifications. Stop signaling is native. Keyboard mode retains portable polling.
- Debounces reconnection, combines logical HID interfaces, cancels obsolete
  requests and limits retries. Initial startup and resume establish a silent
  baseline so launching the helper does not take the screen from another input.
- Offers pairing, diagnostics, start/stop, status and per-user login startup.

Hub mode must be physically verified with your switch: its upstream hub must
disconnect from one host and reconnect to the other when the button is pressed.
Unplugging a keyboard downstream does not change hub ownership. Unplugging or
powering off the whole switch still looks like hub removal; this is not a literal
button-click API. Never fall back silently from hub mode to keyboard mode.

Keyboard mode detects the selected peripherals instead. Unplugging those
peripherals can switch inputs, so it is unsuitable when that behavior is unwanted.

## Install from source

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/loftspace/hdmi-switcher.git
cd hdmi-switcher
uv sync --frozen
uv run hdmi-follow --version
```

Native release ZIPs, once released, include the runtime. Extract into a permanent
user folder. On Windows keep **both** `hdmi-follow.exe` and
`hdmi-follow-background.exe` together. The first is the console command and the
second provides login startup without a console window. Builds are unsigned;
the macOS binary is not notarized.

## Set up Windows-to-Mac

For Windows switch-hub mode, first identify the hub instance in Device Manager
or `uv run hdmi-follow usb-instances`. That command lists all present USB device
instances, not just hubs. Confirm it is the switch's hub, then test it:

```powershell
uv run hdmi-follow diagnose --seconds 60 --switch-hub 'USB\VID_0557&PID_2414'
```

Unplug/replug only the keyboard: the hub must remain present. Press the switch
button to the other host and back: the hub must disappear and return. If it does
not, this detection mode is unsupported by that switch. After verifying, configure:

```powershell
uv run hdmi-follow setup --tv-ip 192.168.0.20 --own-input HDMI_1 --away-input HDMI_2 --switch-hub 'USB\VID_0557&PID_2414'
```

Hub mode uses native Windows Configuration Manager inventory, with no device
handles, drivers, administrator rights or PowerShell polling. It matches the
USB VID/PID across ports when exactly one matching hub is present. If you have
two identical hubs, use the full instance ID to select one. Hub mode is Windows-only.

For explicit keyboard/receiver mode:

Find the devices that really move through your switch:

```sh
uv run hdmi-follow devices
uv run hdmi-follow diagnose --seconds 30
```

During diagnostics, switch to the Mac, wait several seconds, then switch back.
The output lists changed vendor/product IDs only, not keyboard or mouse input.
Choose a device that disappears and returns. Tracking one keyboard or receiver
is usually enough. If multiple devices are selected, all must be present to count
as owned by this computer.

Example only, replace the TV address, input numbers and USB IDs:

```sh
uv run hdmi-follow setup --tv-ip 192.168.0.20 --own-input HDMI_1 --away-input HDMI_2 --device 3434:0e21
uv run hdmi-follow pair
uv run hdmi-follow inputs
uv run hdmi-follow run
```

Accept the pairing prompt on the TV using its remote. Existing configurations
are not replaced unless `setup --overwrite` is supplied. An existing pairing key
can be imported with `pair --import-key-file PATH`; it is checked against the TV
before being saved. Do not put a key on the command line itself.

After checking a few real switches, enable background use:

```sh
uv run hdmi-follow install
uv run hdmi-follow start
uv run hdmi-follow status
uv run hdmi-follow stop
uv run hdmi-follow uninstall
```

`install` registers Windows login startup. On macOS it also starts the LaunchAgent.
`uninstall` removes our login entry and requests a stop; local configuration and
pairing data are retained. `start` is explicit and does not select an input until
the first stable ownership transition.

On Windows, `start` requests job breakaway and warns if the child still belongs
to a Windows job. A launching host can control nested job lifetimes; successful
creation alone does not guarantee independence. Prefer the Windows login task
for persistent operation. Its failure policy retries three times at one-minute
intervals; a normal CLI stop exits successfully and does not trigger those retries.

## Add the Mac later

See [Mac handoff](docs/macos-handoff.md). Install the same helper with the Mac's
input as `--own-input`, and omit `--away-input` for arrival-only operation. That
way the Mac selects itself when it gains the keyboard/mouse; Windows can still
select the Mac on removal. These duplicate commands select the same target.
Physical Mac testing remains a separate gate.

Use `uv run hdmi-follow switch HDMI_1` for an explicit manual selection. `inputs`
prints LG's connected-input data so labels and active signal can be checked.

## Local files and limits

Configuration defaults to `~/Documents/HDMI Follow/config.json` on Windows and
`~/Library/Application Support/HDMIFollow/config.json` on macOS. Use a separate
directory per profile with `--config PATH`. Pairing data is stored beside it in
`credentials.json`. Do not upload that file or your profile directory.

`follow.log` rotates at 2 MB with four backups (about 10 MB total). It records
startup version/PID, native USB actions, inventory and ownership changes, baseline
resets, skipped-command reasons, command attempts and TV request acknowledgements.
A health entry every 30 seconds records ownership state, scheduling gaps and
available RAM/commit memory on Windows. Routine successful health pings and
unchanged one-second inventory checks do not each produce a log entry.
It never records input reports, pairing keys, raw TV frames or server error bodies.
`status` reports process locking
and login registration, not proof that the TV or USB signal is working.

If switching fails, note the approximate time and direction before restarting.
Keep `follow.log` and its numbered backups: the startup PID and ownership
generation connect USB decisions to TV attempts, and each TV request has an ID.
Logging explains observed decisions; an acknowledgement does not prove the TV
showed the picture. A stalled process cannot write during its stall, so its next
health/sample entry records the gap after it resumes.

`run-state.json` persists the run PID, parent PID, last heartbeat and clean exit
reason. On the next start a surviving running record is logged as an unclean
exit with an unknown cause. Relevant Windows crash, resource-exhaustion and host
service records are checked in the background and logged as supporting evidence,
not proof that an unrelated event killed the helper. Stop requests record the
requesting PID, timestamp and command type. `crash.log` captures fatal stack traces
without local variable values and rotates at 1 MB with one backup.

Forced termination and power loss cannot execute an in-process exit handler.
When Windows has no explanatory event, the reason remains explicitly unknown.

The TV must be reachable on the same trusted LAN. LG uses a self-signed TLS
certificate; pairing does not establish verified server identity. The helper
disables system proxy use and never sends TV requests through a web proxy.
It does not wake the TV or change your privacy configuration. If pairing is
revoked, use `pair` again. A TV may display a new pairing prompt before rejecting
an obsolete key; the background helper will not wait for approval or keep retrying.

No claim of instant or atomic switching: an input command already delivered to
the TV cannot be recalled. Windows hub mode reacts to native USB notifications;
missed notifications recover through a one-second inventory check plus debounce.
This fallback does not delay the normal notification path. Short post-event
checks cover arrival notifications delivered before present-device inventory updates.
Keyboard mode scans at a 0.1-second interval. Stabilization defaults to 0.2 seconds
and can be reduced to 0.05 seconds in the profile after hardware validation,
followed by TV/network response and HDMI display negotiation. A warm
connection avoids the first authentication handshake. After a disconnect or TV
standby, the next command may need a fresh connection. Idle ping/pong checks run
every five seconds; retries are bounded and canceled when ownership changes.
Hub mode uses native suspend/resume notifications to reset its baseline silently.
HID enumeration failure or a long sleep/resume gap does the same in keyboard mode.
This avoids stale takeovers but can intentionally miss a switch during recovery.
The helper must be running on an awake computer to observe USB changes.

## Development

```sh
uv sync --frozen --group dev
uv run ruff check .
uv run mypy src
uv run pytest
uv build
uv run python scripts/build.py
```

CI runs on Windows, macOS and Linux. Packaging runs natively on Windows x64 or
an Apple Silicon Mac. Releases are generated after approved merges; an existing
version cannot be overwritten. Automated tests use a local WebSocket TV and
simulated USB metadata, alongside separate real-device checks.
