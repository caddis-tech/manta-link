# Aquadrone time responder

A BlueOS extension that answers the Aquadrone Pico's boot-time request with the
Pi's clock, so every telemetry record can carry an absolute timestamp instead of
uptime alone.

Firmware side: [AquadronePicoFirmware](https://github.com/caddis-tech/AquadronePicoFirmware) issue #86.

**This is not the bridge.** No MAVLink, no disk queue, no network, no
caddis-api. It reads one token and writes one line.

## What it does

```
Pico -> Pi   TIME?
Pi   -> Pico TIME 1754422392123
```

The reply is milliseconds since the Unix epoch. The Pico asks only during its
boot window and stops permanently on the first accepted answer, so this link
carries nothing for the rest of a deployment.

## Two things it deliberately refuses

**It never opens a port it has not identified.** The Pico is matched by USB
vendor ID `0x2E8A`, not by taking the first `ttyACM`. The ArduPilot autopilot
enumerates as the same CDC ACM class, so picking by enumeration order means
sometimes opening the flight controller, which is a port nothing here has any
business writing to.

**It stays silent when the clock is not trustworthy.** The Pi has no
battery-backed RTC; it learns the time from NTP over cellular. A Pi that boots
without a connection reports something near 1970. Stamping good sensor data with
1970 is worse than carrying no timestamp at all, because a wrong number gets
used downstream and a missing one gets noticed.

That silence is the mechanism, not just a safety check. It is why the Pico polls
rather than asking once: nobody can predict the moment NTP disciplines the
clock, so there is nothing for a single request to be timed against. The Pico
keeps asking, this stays quiet, and the first answer is the first one worth
having.

The check reads the kernel's sync state via `adjtimex` rather than asking
systemd via `timedatectl`, because this runs in a container where there is no
systemd to ask. That works the same under `systemd-timesyncd`, `chrony` and
`ntpd`.

## Release

Tag it. That is the whole deploy story.

```bash
git tag v0.1.0
git push --tags
```

GitHub Actions builds `linux/arm/v7` and `linux/arm64` and pushes to
`ghcr.io/caddis-tech/aquadrone-time-responder`. The package is public, so the
boat pulls it with no credentials.

## Install on a boat

BlueOS web UI, Extensions, then install from:

```
ghcr.io/caddis-tech/aquadrone-time-responder:latest
```

**BlueOS 1.4.3's Kraken has no offline extension install**, so the boat needs a
route to ghcr.io to install or update. That makes modem-up a prerequisite for
*installing* this, though not for running it once installed.

**Verify the manifest labels in the `Dockerfile` against the BlueOS version on
the boat.** The extension label schema has changed across BlueOS releases, and
Kraken silently refuses an extension whose manifest it cannot parse. If an
install fails without a useful message, suspect that block first.

## Checking it works

On the Pi:

```bash
docker logs -f $(docker ps -q --filter name=time-responder)
```

A healthy boot logs `listening on /dev/ttyACM0`, then some number of
`request received, clock not yet synced; silent` while the modem connects, then
one `answered with 1754422392123`, then nothing further for the rest of the run.
**That final silence is correct and is the whole point.**

On the Pico's serial stream:

```
Boot time synced: epoch 1754422392123 ms at 47231 ms uptime
```

If it never syncs, the Pico says so plainly and keeps logging on uptime:

```
WARN: no time from the Pi in 180000 ms; records will carry uptime only
```

Records from an unsynced run carry `"epoch_ms":"unsynced"` rather than a zero,
because zero is a real epoch and nothing would distinguish it from an answer
that never came.

## Deployment coupling

The firmware and this extension ship together. A boat running the firmware
without this installed waits out the full 180 second boot window on every
startup before it begins logging, then records `"epoch_ms":"unsynced"` on every
line. Everything else, all sensors and the SD write path, is unaffected: that is
exactly the behaviour the firmware had before the feature existed, plus a delay.

## Testing without a boat

Anything that can open the serial port can answer. To exercise the Pico half
from a desktop, open its port at **115200** (never 1200, see below), wait for a
line reading `TIME?`, and write back `TIME ` followed by the current epoch in
milliseconds.

**Never open the port at 1200 baud.** On a Pico with the stock stdio settings,
that reboots it into BOOTSEL: the board stops being a serial device, comes back
as mass storage, and logging stops with nothing written to say why. The current
firmware disables it, but this stays pinned regardless, since the reset is
triggered by the host's choice rather than the firmware's.
