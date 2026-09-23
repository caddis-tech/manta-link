# usb-stall-test

Reproduces the 2026-09-22 serial-link stall on demand, so MANTA Link's silence
reset can be tested before a release. **Never flash this to a boat.**

It prints `usb-stall-test line N` every 5 s over USB serial, as the flight
firmware does. After 12 lines it wedges its own USB transmit endpoint. The Pico
stays enumerated and the port stays open, but no bytes arrive and nothing
reports an error, which is what the boat did. A USB reset clears the wedge,
and the wedge returns 12 lines after each reconnect, so one flash shows repeated
recoveries. The line numbers skip across each wedge, which shows what was lost.

## Build and flash

Needs the Pico SDK (`PICO_SDK_PATH`) and the ARM toolchain:

```bash
cmake -S tools/usb-stall-test -B build-stall -DPICO_BOARD=pico
cmake --build build-stall
```

On Windows, run those from an MSVC developer environment with `-G Ninja`, and
keep the build directory short (for example `C:/b/stall`): the SDK builds host
tools that fail on long paths. Hold BOOTSEL while plugging the Pico in, then
copy `usb_stall_test.uf2` to the `RPI-RP2` drive.

## Test the silence reset

On a Pi running the MANTA Link build under test, with the installed extension
disabled so only one process owns the port:

1. Plug in the Pico running this tool.
2. In MANTA Link's log, `heartbeat: ... pico_log_lines=` climbs by 12 a minute.
3. After the 12th line it stops climbing. 60 s later:
   `no bytes from /dev/ttyACM0 for 60s; resetting the USB device`, then
   `reset USB device /dev/bus/usb/...` and `listening on /dev/ttyACM0`. On the
   Pi, `dmesg` shows `reset full-speed USB device number N`.
4. `pico_log_lines` climbs again. Let it run through at least three wedges.

Each wedge costs about a minute of lines, as a real stall would. On a boat those
lines are on the SD card.
