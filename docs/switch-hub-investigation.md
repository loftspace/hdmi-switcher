# Switch-specific detection and latency

Version 0.1.1 replaces the deployed keyboard-only strategy with an optional
Windows USB hub identity signal and a reusable LG connection.

## Why the keyboard trigger was insufficient

The old profile watched a Keychron keyboard identity. Both pressing the switch
and simply unplugging that keyboard removed the same device. Software could not
distinguish those actions from that signal alone.

Windows exposes the ATEN switch hub separately. The candidate backend uses the
full hub device instance identity with CM_Get_Device_ID_List_SizeW and
CM_Get_Device_ID_ListW, filtered to PRESENT devices on the USB enumerator.
Keyboard HID devices are not consulted in this mode. Native enumeration measured
about 0.7 ms locally. API failures and size races are never reported as removal.

This is hub ownership detection, not a confirmed button event or vendor control
protocol. It requires the upstream hub to move between hosts with the button.
Unplugging the entire switch remains indistinguishable from switching away.

## Faster network path

The old logs showed about 2-3 seconds from the debounced ownership event to TV
acknowledgement, in addition to the old one-second debounce and 0.5-second polling.
The helper previously created and authenticated a new WebSocket for every input
command. Version 0.1.1 keeps one authenticated connection, sends idle health pings
and assigns unique command IDs. Interrupted commands discard the connection so
old replies cannot satisfy a later request. A closed connection gets a bounded
clean retry only while the ownership signal remains current.

New defaults are 0.2-second debounce and 0.1-second polling. HDMI signal/display
negotiation is outside the script and cannot be removed by lowering its timers.
Real total button-to-picture latency must be measured on the hardware.

## Required physical validation

1. Keyboard-only unplug/replug must leave the hub present and not select an input.
2. Switch button PC -> Mac -> PC must move the hub and select HDMI 2 then HDMI 1.
3. Repeat fast switches and verify no delayed command steals the display back.
4. Stop/start and sleep/resume establish a silent baseline; no automatic takeover.

The previous version's physical round trip does not prove this new hub signal.
Do not mark the new hardware gate passed until its diagnostic events are captured.
