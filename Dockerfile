# BlueOS Extension: MANTA Link
#
# The single Pi-side process that owns the Pico's USB serial port. Built and
# pushed by .github/workflows/publish.yml on a version tag. To build by hand:
#
#   docker buildx build --platform linux/arm/v7 -t <registry>/manta-link:dev --push .
FROM python:3.11-slim-bookworm

RUN pip install --no-cache-dir pyserial==3.5

COPY manta_link/ /app/manta_link/

WORKDIR /app

# Unbuffered, so `docker logs` shows what happened during a boot rather than
# holding it until the buffer fills. This runs for hours and says very little,
# which is exactly the case where buffering hides everything that matters.
ENV PYTHONUNBUFFERED=1

# Where the persistent volume is mounted inside the container. The token, when
# there is one, is read from $AQUADRONE_DATA_DIR/.env, and the spool lives in a
# subdirectory rather than here: nothing may enumerate the directory holding a
# credential.
ENV AQUADRONE_DATA_DIR=/app/data

# --- BlueOS extension manifest ------------------------------------------------
#
# Written against https://blueos.cloud/docs/stable/development/extensions/
#
# Only `version` and `permissions` are required; the rest populate the listing
# in the Extensions Manager. `version` must be SemVer. `type` must be one of
# device-integration, tool, other, example. `tags` are lowercase alphanumeric
# with dashes, ten at most.
#
# Kraken refuses a manifest it cannot parse and does not say why, so CI parses
# every LABEL below as JSON and asserts this version matches the git tag.
LABEL version="0.3.0"

# Privileged with a /dev bind is verbatim what the docs prescribe for reaching
# connected serial devices, and it is what any container needs to open a USB CDC
# device whose path is not fixed.
#
# The process narrows that access itself, by matching USB VID 0x2E8A rather than
# opening whatever ttyACM it finds first. The ArduPilot autopilot is the same CDC
# ACM class and must never be written to, and privileged mode is exactly the
# situation where that mistake would succeed.
#
# NetworkMode host: MAVLink2Rest is on 127.0.0.1:6040 and the upload leg needs
# the host's cellular default route.
#
# /media with rslave: the telemetry archive lands on a removable USB device so it
# can be carried off the boat and read like the Pico's own card. No such device
# exists yet, and the archive ships disabled until one appears, but the bind must
# be here from the start so a stick inserted later becomes visible without
# recreating the container. rslave only propagates if the host's /media mount is
# itself shared or slave; systemd normally makes / rshared, but verify with
# `findmnt -o TARGET,PROPAGATION / /media` on the boat.
#
# Deliberately absent:
#   ExtraHosts    Under host networking the container shares the host namespace,
#                 nothing here resolves blueos.internal, and unjustified keys
#                 cost a boat install to validate.
#   LogConfig     Kraken overwrites it unconditionally with json-file/20m/3, so
#                 declaring mode=non-blocking here does nothing. Log-delivery
#                 backpressure has to be handled in-process instead.
LABEL permissions='{\
  "HostConfig": {\
    "Privileged": true,\
    "NetworkMode": "host",\
    "Binds": [\
      "/dev:/dev:rw",\
      "/usr/blueos/extensions/manta-link:/app/data:rw",\
      "/media:/media:rw,rslave"\
    ],\
    "RestartPolicy": {"Name": "unless-stopped"}\
  }\
}'

LABEL authors='[{"name": "Caddis Tech", "email": "michael.klobutcher@gmail.com"}]'
LABEL company='{"name": "Caddis Tech", "about": "Aquadrone", "email": "michael.klobutcher@gmail.com"}'
LABEL type="device-integration"
LABEL tags='["aquadrone", "telemetry", "water-quality", "serial", "time"]'
LABEL links='{"github": "https://github.com/caddis-tech/manta-link"}'
LABEL readme="https://raw.githubusercontent.com/caddis-tech/manta-link/main/README.md"
LABEL requirements="core >= 1.1"

# No HEALTHCHECK, deliberately. Any check keyed on record freshness restarts the
# container on a flight boat, where a Release image sends no records over USB at
# all and silence is the correct steady state. A restart is also the one event
# that can lose an in-flight TIME?, which is the request this process exists to
# answer and which the Pico only asks during its first three minutes.

ENTRYPOINT ["python3", "-m", "manta_link"]
