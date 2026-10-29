# Continue on the Mac

This part is optional: the Windows helper can select the Mac on USB removal and
select Windows on arrival, while the PC is awake. A Mac helper lets the Mac claim
its own input independently when the PC is asleep or off.

The Mac implementation is built alongside Windows, but the author's physical Mac
has not been available for installation or USB-switch verification. CI checks on
an Apple Silicon runner are not a substitute for testing the actual switch.

## First verification session

1. Clone this repository and check out the feature PR branch. Install Python 3.12
   or newer and `uv`, then run `uv sync --group dev`.
2. Run `uv run hdmi-follow --help` and the device-list/diagnostic commands shown
   in the README. Confirm that the switched keyboard or receiver appears when
   switching to the Mac and disappears when switching to Windows.
3. Configure the TV address, pair from the Mac and confirm the TV's prompt with
   its remote. The PC's credentials must not be pasted into the repository.
4. Select the Mac's actual HDMI input. Leave Mac switching arrival-only while
   testing, so a disconnect does not compete with the Windows helper's command.
5. Run interactively and physically switch at least five times in each direction.
   Verify that the correct HDMI appears once per switch. Check TV-unreachable
   behavior, fast switches, keyboard unplug/replug and sleep/wake.
6. Install the login entry only after the interactive tests pass. Test a fresh
   login, and test stop/removal. Do not claim Mac hardware support based solely on CI.

The macOS login entry is a user LaunchAgent named `org.loftspace.hdmi-follow` in
`~/Library/LaunchAgents`. It runs the same virtual-environment interpreter or
native executable that installed it, without shell command interpolation. Keep
that installation path stable. Removing the login entry retains configuration
and pairing files.

Native macOS releases are currently Apple Silicon only, unsigned and not
notarized. Build them on an Apple Silicon Mac with `uv run python scripts/build.py`.
Never label a Windows-built executable as a Mac build. No administrator rights
are needed for the user LaunchAgent.
