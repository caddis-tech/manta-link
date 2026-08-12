# MANTA Link

The single Pi-side process that owns the Aquadrone Pico's USB serial port.

Only one process can usefully read a tty. The kernel hands each byte to exactly
one reader, and pyserial rewrites the shared termios on every open, so a second
opener both steals bytes and silently changes the survivor's read timing. This
extension therefore owns the port outright and does every Pi-side job that needs
it, rather than letting a second program compete for the same device.

Today that is answering the Pico's boot-time request, so every telemetry record
carries an absolute timestamp instead of uptime alone, and making the records it
reads durable: a spool for the upload to drain, and an archive that keeps a copy
on the boat. GPS enrichment and the upload itself land on top of the same
reader.

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
| `mavlink2rest.py` | One HTTP GET, and the shapes MAVLink2Rest wraps a value in |
| `gps.py` | Whether a fix is worth believing, and caching the last one that was |
| `capture.py` | Draining the reader's buffers and parsing, on its own thread |
| `record.py` | The envelope, the timestamp policy, and the field mapping |
| `spool.py` | The durable queue the upload drains, and where it lives |
| `archive.py` | The rotating NDJSON copy that stays on the boat |
| `health.py` | Counters, the heartbeat, and restarting a worker that died |
| `logging_setup.py` | The log queue, and the one thread that writes to stdout |
| `supervisor.py` | Keeping a loop alive through anything that is not a shutdown |
| `__main__.py` | Signal handling, and running the reader on the main thread |

The reader hands a line off by appending it to a bounded `deque` and goes
straight back to `read()`. At capacity `deque.append` drops the oldest under the
GIL, so a stalled worker costs old records and can never cost a reply, which is
the thing that cannot be had twice. Every thread logs through a bounded queue
for the same reason: a `StreamHandler` writes and flushes while holding a lock
that every thread shares, and Docker's log delivery blocks when nothing is
draining its pipe. Lines are dropped when that queue fills, and the count of
dropped lines is reported once a minute.

Health is a worker as well, so the main thread checks on it from inside the
reader loop. Nothing inside the worker set could notice health dying, and its
death would quietly take the watchdog off every other worker.

## What happens to a record

A parsed record becomes an envelope, is appended to the archive, and is written
to the spool. The archive goes first, so a spool write that fails still leaves
the position on the stick.

**The Pico is the source of truth on timestamps.** It stamps at sampling time
and we can only stamp at receipt, one to three seconds later, so this fills gaps
and never overrides. A record with `epoch_ms` keeps it. A record without one is
stamped from the run's boot anchor, if there is one. A record with neither is
spooled unstamped and becomes eligible the moment an anchor for *its own run*
arrives: every entry carries the identity of the boot its uptime counts from,
because stamping one from a later run's anchor shifts it by the gap between the
two boots and nothing afterwards can tell.
Nothing unstamped is ever uploaded: the API's `timestamp` falls back to *ingest*
time, so a backlog draining after an outage would land every reading at the
moment it drained, and there is no correcting it afterwards because a
resubmitted `client_ref` returns `duplicate` rather than an update.

The anchor is dropped whenever it stops describing the run in front of us: on
any serial reconnect, on a boot banner, and on an uptime that does not rise. The
reconnect is the reliable one, because any Pico reset forces a USB
re-enumeration, while a banner can be printed into a deasserted DTR and never
arrive. After a drop it re-derives from the next record rather than waiting for
a banner or a sync line, since a run whose Pi clock was undisciplined at boot
emits neither.

**The payload is transformed, not copied.** caddis-api stores whatever it is
sent but reads named properties over fixed keys, and the Pico's names are not
those keys: `cond_tds_sal` is one string where the API reads `conductivity`,
`tds` and `salinity` as separate numbers, `ph` arrives as a string, and the API
reads `water_temperature` rather than `temperature`. A verbatim copy returns
`created` and reads back empty, with nothing in the write path to say so. Keys
the API cannot read still travel in the blob, and one the Pico has never sent
before is counted and named in the log rather than passed through in silence.

`client_ref` is `uuid5` of the raw Pico line, so every copy of one reading
resolves to the same row: the live upload, a retry after a lost acknowledgement,
the archive stick and the Pico's own card.

## Where the boat was

The Pico has no GPS, so every `gps_latitude` that reaches caddis-api comes from
`gps.py` polling MAVLink2Rest. A record gets `gps_latitude`, `gps_longitude`,
`gps_age_s`, `gps_fix_type`, `gps_satellites` and `gps_hdop`, or it gets none of
them: an absent key and a null key read the same to every consumer, and the link
is metered. **caddis-api has a reader property for the first two only.** The
other four ride in the payload blob and are invisible until one is added, which
is the same gap `uv_index` has.

Two staleness questions are answered in two places, because they fail
differently. *Is the autopilot still talking?* Only `status.time.counter` moves;
MAVLink2Rest serves the last message it received forever, so a 200 says the
service is up and nothing else. That is `gps.py`. *Is this snapshot current?*
The poller publishes the instant a fix was accepted, never an age, and
`record.py` measures it against the moment each record was received. A poller
wedged in a socket read stays alive so the watchdog never restarts it, and the
capture worker can be draining a record buffered eleven minutes ago; only a
per-record age notices either.

A fix has to be 3D or better. 2D is refused because the only consumer is a
strict ray-cast containment with no low-confidence channel, so a 2D fix under
poor geometry files a sample in the neighbouring pond, permanently. The refused
coordinate is still written to the archive, because refusing to *file* it is
defensible and refusing to *record* it is a one-way loss. A position of exactly
(0, 0) is discarded outright: ArduPilot publishes it until the EKF has an
origin, and caddis-api ray-casts it like any other point.

The spool lives in a subdirectory of the extension volume, never the volume
itself, because `.env` holds the API token and the startup index scan must never
enumerate the directory holding it. One scan at startup builds an in-memory
index, and nothing globs after that. Filenames are a monotonic sequence rather
than a wall-clock prefix, so an NTP step backwards cannot invert eviction order.
Evictions and discards are counted and carried on the heartbeat, because the API
is the system of record and whatever the spool drops is missing from the dataset
for good.

The archive is a rotating NDJSON ring on a removable device. It is not redundant
with the Pico's card: it holds GPS position, which the Pico knows nothing about,
and it comes off the boat without opening the enclosure. **No boat has a stick
yet, so it ships disabled** and says so once on the heartbeat. Set
`AQUADRONE_DATA_DEVICE`, or plug in a stick carrying an `aquadrone` directory,
and it turns on.

## Install on a boat

BlueOS web UI, Extensions, **Installed** tab, the **+** button, then:

| Field | Value |
|---|---|
| Extension Identifier | `caddis.manta-link` |
| Extension Name | `MANTA Link` |
| Docker image | `ghcr.io/caddis-tech/manta-link` |
| Docker tag | a pinned version such as `0.9.0` |
| Custom settings | leave empty; the image's own `permissions` label is used |

Pin the tag rather than using `latest`, so the Extensions Manager shows which
build a boat is actually running.

Requires BlueOS 1.4.x. Images are published for `linux/arm/v7` and
`linux/arm64`, and the package is public, so the boat pulls it without
credentials. Kraken has no offline install, so the boat needs a route to
ghcr.io to install or update, though not to keep running once installed.

### The API token

**Install without one.** A missing token is a normal state, not an error:
nothing POSTs, nothing spins, and capture, spooling and `TIME?` all carry on.
Uploads begin whenever a token turns up. Getting the extension running and
provisioning it are two separate jobs and do not have to happen in one visit.

The token goes in a `.env` on the extension's persistent volume:

```bash
ssh <boat> 'printf "CADDIS_API_TOKEN=%s\n" "<token>" > /app/data/.env'
```

`/app/data` is the default; `AQUADRONE_DATA_DIR` overrides it. The file is read
again on every heartbeat, so a token dropped in starts uploads **within a minute
with no restart**, and a 401 or 403 self-heals the same way once a good one
replaces it.

**Write it over SSH, not through Commander's `rig()` helper.** That helper is
`curl -G --data-urlencode`, so the token would land in a query string, in shell
history, and in Commander's request log.

`CADDIS_API_TOKEN` is also read from the environment, which means Kraken's Env
field works too. The file wins if both are set. Kraken's field is visible in the
BlueOS UI to anyone who can reach it, so prefer the file.

Two other knobs, both optional: `CADDIS_API_URL` (default
`https://api.caddistech.com`) and `CADDIS_BATCH_MAX` (default 50).

#### Rotation

Overwrite the line. The next heartbeat rebinds, and the log says so:

```
API token changed from 3f9a1c2e to 8b4d0e77 (source: file)
```

Those are the first 8 characters of a SHA-256, not the token. **The token is
never logged**, and neither is any transport error message, because a `requests`
exception can carry the header it failed on.

Rotation replaces the whole session rather than re-heading a live one, so an
in-flight POST finishes on the credential it started with. There is no window
where a retired token is still in use, which is the failure this design exists
to prevent: it is silent, and the operator believes the old credential is dead.

### Replacing the time responder

**On a boat, install this one first**, confirm it is running, then uninstall
`caddis.aquadrone-time-responder`. The reverse order leaves a boat with nothing
on the port if the pull fails, which over a cellular link is not recoverable
from shore. The brief overlap is harmless because the Pico is already synced by
then.

**On a bench rig, do it the other way round**: uninstall the responder first,
then install this. That advice is not a contradiction, it is a different
question. The order above trades a certain small risk for an uncertain large
one, and the large one is "no route back to the boat" -- which does not exist on
a rig you can reach with a cable. What is left is the overlap, and on a rig the
overlap is the real risk: two processes both want one tty, `exclusive=True` is
advisory flock, and `TIOCEXCL` is defeated by the `CAP_SYS_ADMIN` this container
holds. `TIME?` requests then split non-deterministically between them and every
log line afterwards is worthless.

Then power-cycle the Pico to prove the `TIME?` path end to end.

## Checking it worked

On the Pi:

```bash
docker logs -f $(docker ps -q --filter name=manta-link)
```

A healthy boot logs `listening on /dev/ttyACM0`, then some number of
`request received, clock not yet synced; silent` while the Pi gets online, then
one `answered with 1754422392123`. After that it says almost nothing: a
`heartbeat: ...` line of counters every minute, `still listening` every ten,
and on a Debug image a count of the records captured. **That near-silence is
correct and is the whole point**; the periodic lines exist so a wedged port and
a dead worker look different from a quiet boat.

With no token it also logs `no API token configured; uploads are off` once, and
that is the whole of it. It does not retry, does not back off, and does not
count that as a failure, so an unprovisioned boat looks exactly as quiet as a
provisioned one. Confirm it is still *working* from the heartbeat's spool
counters rather than from the absence of upload lines.

When a token arrives, one `API token loaded, fingerprint 3f9a1c2e (source:
file)` and the spool starts draining. Two counters on the heartbeat are the ones
to read: `spool_write_failures` above zero means a read-only bind and the boat is
storing nothing, and `readings_quarantined` above zero means a batch the API kept
refusing has been set aside so the rest of the queue can move.

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
git tag v0.9.0
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
