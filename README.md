# Aquadrone time responder

A BlueOS extension that answers the Aquadrone Pico's boot-time request with the
Pi's clock, so every telemetry record carries an absolute timestamp instead of
uptime alone.

Firmware side: [AquadronePicoFirmware](https://github.com/caddis-tech/AquadronePicoFirmware) issue #86.

## What it does

```
Pico -> Pi   TIME?
Pi   -> Pico TIME 1754422392123
```

The reply is milliseconds since the Unix epoch. The Pico asks only during its
boot window and stops permanently on the first accepted answer, so this link
carries nothing for the rest of a deployment.

It reads one token and writes one line. No MAVLink, no queue, no network.

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
`ntpd`.

## Install on a boat

BlueOS web UI, Extensions, **Installed** tab, the **+** button, then:

| Field | Value |
|---|---|
| Extension Identifier | `caddis.aquadrone-time-responder` |
| Extension Name | `Aquadrone Time Responder` |
| Docker image | `ghcr.io/caddis-tech/aquadrone-time-responder` |
| Docker tag | `latest`, or a pinned version such as `0.1.0` |
| Custom settings | leave empty; the image's own `permissions` label is used |

Requires BlueOS 1.4.x. Images are published for `linux/arm/v7` and
`linux/arm64`, and the package is public, so the boat pulls it without
credentials.

Kraken has no offline install, so the boat needs a route to ghcr.io to install
or update. Being online is a prerequisite for *installing* this, not for running
it once installed.

## Checking it worked

On the Pi:

```bash
docker logs -f $(docker ps -q --filter name=time-responder)
```

A healthy boot logs `listening on /dev/ttyACM0`, then some number of
`request received, clock not yet synced; silent` while the Pi gets online, then
one `answered with 1754422392123`, then nothing further for the rest of the run.
**That final silence is correct and is the whole point.**

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

## Deployment coupling

The firmware and this extension ship together. A boat running the firmware
without this installed waits out the full 180 second boot window on every
startup before it begins logging, then records `"epoch_ms":null` on every line.
Everything else, all sensors and the SD write path, is unaffected: that is
exactly the behaviour the firmware had before the feature existed, plus a delay.

## Release

Tag it. That is the whole deploy story.

```bash
git tag v0.1.0
git push --tags
```

GitHub Actions builds both architectures and pushes to
`ghcr.io/caddis-tech/aquadrone-time-responder`.

## Testing without a boat

Anything that can open the serial port can answer. To exercise the Pico half
from a desktop, open its port at **115200**, wait for a line reading `TIME?`,
and write back `TIME ` followed by the current epoch in milliseconds.

**Never open the port at 1200 baud.** On a Pico with the stock stdio settings
that reboots it into BOOTSEL: the board stops being a serial device, comes back
as mass storage, and logging stops with nothing written to say why. The current
firmware disables it, but this stays pinned regardless, since the reset is
triggered by the host's choice rather than the firmware's.
