# HDMI Follow

**Press your USB switch. Let your LG TV follow.**

[![CI](https://github.com/loftspace/hdmi-follow/actions/workflows/ci.yml/badge.svg)](https://github.com/loftspace/hdmi-follow/actions/workflows/ci.yml)

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
git clone https://github.com/loftspace/hdmi-follow.git
cd hdmi-follow
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
uv run hdmi-follow diagnose --seconds 60 --switch-hub 'USB\VID_0557&PID_2414\YOUR_INSTANCE'
```

Unplug/replug only the keyboard: the hub must remain present. Press the switch
button to the other host and back: the hub must disappear and return. If it does
not, this detection mode is unsupported by that switch. After verifying, configure:

```powershell
uv run hdmi-follow setup --tv-ip 192.168.0.20 --own-input HDMI_1 --away-input HDMI_2 --switch-hub 'USB\VID_0557&PID_2414\YOUR_INSTANCE'
```

Hub mode uses native Windows Configuration Manager inventory, with no device
handles, drivers, administrator rights or PowerShell polling. It matches the
full instance identity; moving the upstream cable to another PC port can change
that identity and requires reconfiguration. Hub mode is Windows-only.

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

`follow.log` rotates at 1 MB with two backups. It records ownership and command
outcomes, never input reports or credentials. `status` reports process locking
and login registration, not proof that the TV or USB signal is working.

The TV must be reachable on the same trusted LAN. LG uses a self-signed TLS
certificate; pairing does not establish verified server identity. The helper
disables system proxy use and never sends TV requests through a web proxy.
It does not wake the TV or change your privacy configuration. If pairing is
revoked, use `pair` again. A TV may display a new pairing prompt before rejecting
an obsolete key; the background helper will not wait for approval or keep retrying.

No claim of instant or atomic switching: an input command already delivered to
the TV cannot be recalled, and USB enumeration has a polling interval. A stable
transition uses a 0.2-second stabilization period and 0.1-second polling by
default, followed by TV/network response and HDMI display negotiation. A warm
connection avoids the first authentication handshake. After a disconnect or TV
standby, the next command may need a fresh connection. Idle ping/pong checks run
every five seconds; retries are bounded and canceled when ownership changes.
HID enumeration failure or a long sleep/resume gap resets the baseline silently.
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
