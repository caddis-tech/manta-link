# BlueOS Extension: Aquadrone time responder
#
# Built and pushed by .github/workflows/publish.yml on a version tag. To build
# it by hand instead:
#
#   docker buildx build --platform linux/arm/v7 -t <registry>/aquadrone-time-responder:dev --push .
FROM python:3.11-slim-bookworm

RUN pip install --no-cache-dir pyserial==3.5

COPY time_responder.py /app/time_responder.py

# Unbuffered, so `docker logs` shows what happened during a boot rather than
# holding it until the buffer fills. This runs for hours and says very little,
# which is exactly the case where buffering hides everything that matters.
ENV PYTHONUNBUFFERED=1

# --- BlueOS extension manifest ------------------------------------------------
#
# Written against https://blueos.cloud/docs/stable/development/extensions/
#
# Only `version` and `permissions` are required; the rest populate the listing
# in the Extensions Manager. `version` must be SemVer. `type` must be one of
# device-integration, tool, other, example. `tags` are lowercase alphanumeric
# with dashes, ten at most.
LABEL version="0.1.0"

# Privileged with a /dev bind is verbatim what the docs prescribe for reaching
# connected serial devices, and it is what any container needs to open a USB CDC
# device whose path is not fixed.
#
# The script narrows that access itself, by matching USB VID 0x2E8A rather than
# opening whatever ttyACM it finds first. The ArduPilot autopilot is the same CDC
# ACM class and must never be written to, and privileged mode is exactly the
# situation where that mistake would succeed.
LABEL permissions='{\
  "HostConfig": {\
    "Privileged": true,\
    "Binds": ["/dev:/dev"],\
    "RestartPolicy": {"Name": "unless-stopped"}\
  }\
}'

LABEL authors='[{"name": "Caddis Tech", "email": "michael.klobutcher@gmail.com"}]'
LABEL company='{"name": "Caddis Tech", "about": "Aquadrone", "email": "michael.klobutcher@gmail.com"}'
LABEL type="other"
LABEL tags='["aquadrone", "time", "serial"]'
LABEL links='{"github": "https://github.com/caddis-tech/aquadrone-time-responder"}'
LABEL readme="https://raw.githubusercontent.com/caddis-tech/aquadrone-time-responder/main/README.md"

ENTRYPOINT ["python3", "/app/time_responder.py"]
