# manta-link

This repository follows the Caddis engineering standards. Read them before your first edit,
commit, or pull request: `../../../AGENTS.md` when this repository is cloned into caddis-hq's
`repos/`, as bootstrap does, and https://github.com/caddis-tech/caddis-hq/blob/main/AGENTS.md
otherwise. What follows is only what is specific to this repository.

**If this copy is not inside caddis-hq, say so first.** Look for caddis-hq in the folders above
this one: a folder that holds both `repos.yml` and `AGENTS.md`. If there is none, tell the person
once, at the start, in plain words: this copy was not set up through caddis-hq, so the shared
rules, the step-by-step guides in its `docs/`, and the other repositories this one works with
are not here. The fix is to clone caddis-hq, run its bootstrap script as its README says, and
start again from the copy that makes. Until then, read the standards with
`gh api repos/caddis-tech/caddis-hq/contents/AGENTS.md -H "Accept: application/vnd.github.raw"`,
and if that fails too, say you are working without them.

## Scope

This repo owns the Pico's serial port on the Pi, the spool, the archive ring, the GPS
enrichment, and the upload. It deliberately does not interpret readings: if a fix needs to
know what a reading means, it is not here.

## Before you commit and push

Install once with `pip install -e ".[dev]"`. Before every commit, run the checks:

```bash
ruff check .
mypy manta_link
```

Before every push, run the fast tests:

```bash
pytest
python tools/manifest.py --no-tag
```

Together these are what CI runs.

The suite needs no hardware; `tests/fakes.py` stands in for the port.

**Never open the Pico's port at 1200 baud.** On stock stdio settings that reboots the Pico into
BOOTSEL, and logging stops with nothing written to say why.

## How it ships

A `v*` tag runs `publish.yml`, which builds both architectures and pushes the image to
`ghcr.io/caddis-tech/manta-link`. Boats install it through BlueOS's extension manager. A tag
is a release to boats, so an assistant pushes one only when the person explicitly asks, and says
first that it reaches boats.

`aquadrone-setup` keeps a byte-identical copy of this repo's `Dockerfile` and reads its
`permissions` and `version` labels. Change either and that copy needs the same change; nothing
enforces it yet.
