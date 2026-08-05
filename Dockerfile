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
# VERIFY THESE AGAINST THE BlueOS VERSION ON THE BOAT BEFORE RELYING ON THEM.
# The label schema has changed across BlueOS releases, and Kraken silently
# refuses an extension whose manifest it cannot parse. If an install fails with
# no useful message, suspect this block first.
LABEL version="0.1.0"
LABEL permissions='{\
  "HostConfig": {\
    "Binds": ["/dev:/dev"],\
    "Privileged": true,\
    "RestartPolicy": {"Name": "unless-stopped"}\
  }\
}'
LABEL authors='[{"name": "Caddis Tech"}]'
LABEL company='{"name": "Caddis Tech", "about": "Aquadrone"}'
LABEL type="other"
LABEL tags='["aquadrone", "time", "serial"]'
LABEL readme="https://raw.githubusercontent.com/caddis-tech/aquadrone-time-responder/main/README.md"

# Privileged plus /dev is what gets a container to a USB CDC device whose path
# is not fixed. The script narrows that itself by matching USB VID 0x2E8A rather
# than opening whatever ttyACM it finds, because the ArduPilot autopilot is the
# same device class and must never be written to.

ENTRYPOINT ["python3", "/app/time_responder.py"]
