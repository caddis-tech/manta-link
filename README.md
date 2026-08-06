# MANTA Link

The single Pi-side process that owns the Aquadrone Pico's USB serial port.

Only one process can usefully read a tty. The kernel hands each byte to exactly
one reader, and pyserial rewrites the shared termios on every open, so a second
opener both steals bytes and silently changes the survivor's read timing. This
extension therefore owns the port outright and does every Pi-side job that needs
it, rather than letting a second program compete for the same device.

Today that is one job: answer the Pico's boot-time request, so every telemetry
record carries an absolute timestamp instead of uptime alone. Telemetry capture,
GPS enrichment, spooling and upload land on top of the same reader.

Firmware side: [AquadronePicoFirmware](https://github.com/caddis-tech/AquadronePicoFirmware) issue #86.

## What it does

```
Pico -> Pi   TIME?
Pi   -> Pico TIME 1754422392123
```

The reply is milliseconds since the Unix epoch. The Pico asks only during its
boot window and stops permanently on the first accepted answer, so a missed
answer costs that entire run its absolute timestamps with no second chance.
That is the one obligation everything else in this process is arranged around.

## Two things it deliberately refuses

**It never opens a port it has not identified.** The Pico is matched by USB
vendor ID `0x2E8A`, not by taking the first `ttyACM`. The ArduPilot autopilot
enumerates as the same CDC ACM class, so picking by enumeration order means
sometimes opening the flight controller, which is a port nothing here has any
business writing to.

**It stays silent when the clock is not trustworthy.** The Pi has no
battery-backed RTC and learns the time from NTP once it is online, so a Pi that
boots without a connection reports something near 1970. Stamping good sensor
data with 1970 is worse than carrying no timestamp at all, because a wrong
number gets used downstream and a missing one gets noticed.

That silence is the mechanism, not just a safety check. It is why the Pico polls
rather than asking once: nobody can predict the moment NTP disciplines the
clock, so there is nothing for a single request to be timed against. The Pico
keeps asking, this stays quiet, and the first answer is the first one worth
having.

The check reads the kernel's sync state via `adjtimex` rather than asking
systemd via `timedatectl`, because this runs in a container where there is no
systemd to ask. That works the same under `systemd-timesyncd`, `chrony` and
`ntpd`. It refuses a clock past 2100 as well as one before 2025, because the
firmware rejects both and only says so on one side.

## Layout

| Module | What it owns |
|---|---|
| `reader.py` | The serial port. The only thing that opens or writes to it |
| `framing.py` | Bytes to lines, lines to kinds. Pure, no I/O |
| `clock.py` | Whether this Pi's clock is worth sending, and what it reads |
| `portfinder.py` | Finding the Pico among the boat's USB serial devices |
| `supervisor.py` | Keeping a loop alive through anything that is not a shutdown |
| `__main__.py` | Signal handling, and running the reader on the main thread |

## Install on a boat

BlueOS web UI, Extensions, **Installed** tab, the **+** button, then:

| Field | Value |
|---|---|
| Extension Identifier | `caddis.manta-link` |
| Extension Name | `MANTA Link` |
| Docker image | `ghcr.io/caddis-tech/manta-link` |
| Docker tag | a pinned version such as `0.2.0` |
| Custom settings | leave empty; the image's own `permissions` label is used |

Pin the tag rather than using `latest`, so the Extensions Manager shows which
build a boat is actually running.

Requires BlueOS 1.4.x. Images are published for `linux/arm/v7` and
`linux/arm64`, and the package is public, so the boat pulls it without
credentials. Kraken has no offline install, so the boat needs a route to
ghcr.io to install or update, though not to keep running once installed.

### Replacing the time responder

Install this one **first**, confirm it is running, then uninstall
`caddis.aquadrone-time-responder`. The reverse order leaves a boat with nothing
on the port if the pull fails, which over a cellular link is not recoverable
from shore. The brief overlap is harmless because the Pico is already synced by
then.

Then power-cycle the Pico to prove the `TIME?` path end to end.

## Checking it worked

On the Pi:

```bash
docker logs -f $(docker ps -q --filter name=manta-link)
```

A healthy boot logs `listening on /dev/ttyACM0`, then some number of
`request received, clock not yet synced; silent` while the Pi gets online, then
one `answered with 1754422392123`. After that it logs `still listening` every
ten minutes and nothing else. **That near-silence is correct and is the whole
point**; the periodic line exists so a wedged port looks different from a quiet
one.

On the Pico's serial stream:

```
Boot time synced: epoch 1754422392123 ms at 47231 ms uptime
```

If it never syncs, the Pico says so and keeps logging on uptime:

```
WARN: no time from the Pi in 180000 ms; records will carry uptime only
```

Records from an unsynced run carry `"epoch_ms":null` rather than a zero, because
zero is a real epoch and nothing would distinguish it from an answer that never
came.

If an install fails without a useful message, suspect the `LABEL` block in the
`Dockerfile`: Kraken refuses a manifest it cannot parse and does not say why.
`python tools/manifest.py` checks it before you find out the hard way, and CI
runs the same check against the release tag.

## Deployment coupling

The firmware and this extension ship together. A boat running the firmware
without this installed waits out the full 180 second boot window on every
startup before it begins logging, then records `"epoch_ms":null` on every line.
Everything else, all sensors and the SD write path, is unaffected: that is
exactly the behaviour the firmware had before the feature existed, plus a delay.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
mypy manta_link
```

mypy is pinned to the Linux platform in `pyproject.toml`, because `fcntl` and
`termios` do not exist on Windows and the container is the only platform this
actually runs on.

The test suite needs no hardware. `tests/fakes.py` plays a scripted byte program
back through a stand-in port and can inject the failures that have actually cost
us something: a Pico that stops draining, a device that vanishes mid-read, and
an exception that is not an `OSError`.

## Release

Tag it. CI runs the tests and validates the manifest against the tag before
anything is pushed.

```bash
git tag v0.2.0
git push --tags
```

GitHub Actions builds both architectures and pushes to
`ghcr.io/caddis-tech/manta-link`.

## Testing without a boat

Anything that can open the serial port can answer. To exercise the Pico half
from a desktop, open its port at **115200**, wait for a line reading `TIME?`,
and write back `TIME ` followed by the current epoch in milliseconds.

**Never open the port at 1200 baud.** On a Pico with the stock stdio settings
that reboots it into BOOTSEL: the board stops being a serial device, comes back
as mass storage, and logging stops with nothing written to say why. The current
firmware disables it, but this stays pinned regardless, since the reset is
triggered by the host's choice rather than the firmware's.
