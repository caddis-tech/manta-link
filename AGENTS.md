# manta-link

This repository follows the Caddis engineering standards. Read them before your first edit,
commit, or pull request: `../../../AGENTS.md` when this repository is cloned into caddis-hq's
`repos/`, as bootstrap does, and https://github.com/caddis-tech/caddis-hq/blob/main/AGENTS.md
otherwise. What follows is only what is specific to this repository.

## Scope

This repo owns the Pico's serial port on the Pi, the spool, the archive ring, the GPS
enrichment, and the upload. It deliberately does not interpret readings: if a fix needs to
know what a reading means, it is not here.

## Before you push

Run what CI runs:

```bash
pip install -e ".[dev]"
ruff check .
mypy manta_link
pytest
python tools/manifest.py --no-tag
```

The suite needs no hardware; `tests/fakes.py` stands in for the port.

**Never open the Pico's port at 1200 baud.** On stock stdio settings that reboots the Pico into
BOOTSEL, and logging stops with nothing written to say why.

## How it ships

A `v*` tag runs `publish.yml`, which builds both architectures and pushes the image to
`ghcr.io/caddis-tech/manta-link`. Boats install it through BlueOS's extension manager.

`aquadrone-setup` keeps a byte-identical copy of this repo's `Dockerfile` and reads its
`permissions` and `version` labels. Change either and that copy needs the same change; nothing
enforces it yet.
