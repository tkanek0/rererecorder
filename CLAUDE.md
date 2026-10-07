# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repository is

The **acquisition arm** of the *Multimodal Spatial Awareness* research theme. It
records a RealSense D455 and a ReSpeaker USB Mic Array side by side, on one
clock, so that sound and geometry can be lined up afterwards from the files.

It is not a general-purpose recorder, and optimising it as one is a wrong turn.
Every feature here exists to make a research claim measurable downstream.

| Path | Role |
|---|---|
| `../digital-garden/SpatialAIxHCI/proposals/multimodal_spatial_awareness_proposal.md` | The proposal this serves |
| `../digital-garden/SpatialAIxHCI/survey/datasets.md` | Why public datasets are not enough, and what self-recorded data has to supply |
| `../digital-garden/SpatialAIxHCI/devlog/` | What was built, what it measured, and what it does **not** establish |

## Why self-recorded data exists at all

The survey concluded that placing sound events in 3D is already done by prior
work. Only three differences remain, and public datasets are structurally unable
to supply them:

1. **Working in a real environment.** The AEA sequence in use contains 928 own-voice
   frames out of 969 confident ones and **nothing from behind** - it does not
   contain the phenomenon the project is about. Out-of-view and occluded events
   have to be staged, which means recording them here.
2. **Accumulating as an updatable history**, not per-query snapshots.
3. **Presenting to humans and evaluating by their awareness.** The Aria licence
   restricts public display of dataset materials; recordings made here carry no
   such restriction, so they are what can go in front of subjects and into
   figures.

When a proposed change does not serve one of those three, it probably should not
be built.

## Current direction (2026-09-07)

Agreed with the user:

- **Moving rig**, not a fixed installation - partly to measure what the ReSpeaker
  can actually do while in motion.
- **ReSpeaker stays** for now. Measuring its limits *is* a deliverable.
- **Monocular SLAM** is done outside this repository. The infrared pair and depth
  are recorded as **reference data for validating monocular SLAM**, not as its
  input.
- **A neutral export format**, converted here, is how recordings leave this
  repository. Nothing that reads it imports this package. `video.rrdb` is a
  performance-driven internal format and stays that way.
  `scripts/export.py` writes it and `scripts/validate_export.py` checks it; the
  `rrr` package knows nothing of the layout. The layout is specified in
  `docs/export-format.md`, and why it looks that way in `docs/decisions.md` 17.
- **Lab prototyping only.** No field site yet, so site-level anchoring, capacity
  profiles for long field sessions and redaction are all deferred.

## What the array can and cannot do

Worth knowing before designing any experiment on it, and worth stating in any
write-up. The ReSpeaker is measurably weaker than the Aria array in the AEA
dataset:

| | Aria (AEA) | ReSpeaker USB Mic Array |
|---|---|---|
| widest baseline | 16.6 cm | 9.26 cm (opposite pair, 46.3 mm radius) |
| sample rate | 48 kHz | 16 kHz, fixed |
| max TDOA | 484 us = 23.2 samples | 270 us = 4.32 samples |
| alias-free limit, widest pair | 1034 Hz | 1852 Hz |
| one sample of TDOA, at broadside | about 2.5 deg | about **13.3 deg** |
| geometry | three-dimensional | **planar** - weak in elevation and front/back |

One consolation: the smaller aperture makes the far-field (plane wave)
approximation valid closer in, which is a problem on Aria with near-field
sources.

The practical consequence is that **hand-measured ground truth is good enough**.
A source placed to 10 cm at 2 m is a 2.9 degree reference against a 13 degree
quantisation, so motion capture is not needed to measure this array.

## Design principles

This project is under active development. Keep the code and docs at the ideal,
minimal design for what exists today.
Adhere to **YAGNI**, **KISS**, **SRP**, and **DRY**.

- Remove dead code immediately. Backward compatibility is not required, change
  formats and protocols freely.
- Generalize similar logic aggressively and prefer one simple mechanism over
  special cases.
- Merge similar tests and drop overly fine-grained ones. Test behavior that
  matters, not every branch.
- Keep code comments minimal. When an explanation would get long, put it in
  `docs/` and point there.
- Update the affected docs in the same turn as any change to behavior,
  commands, settings, structure or names, and commit them with that change.
  Before finishing a turn, check that the docs still describe the code.

## Conventions

- The recorder lives under `backend/rrr/`. Import as `from rrr.video import ArchiveSource`.
  Do not add top-level packages - see `docs/decisions.md` 15 - except a device
  adapter that knows nothing of `rrr`: `backend/realsense_adapter/` and
  `backend/respeaker_adapter/` read no environment variable, and
  `rrr.recorder.config` passes their settings in.
- Packaged with `uv_build` (`module-root = "backend"`) and installed editable by
  `uv sync`, in the container too; nothing sets `PYTHONPATH` - see
  `docs/decisions.md` 27.
- The package provides means; `scripts/` decides what to write. A converter's
  encoder or file layout goes in a thin script, the reading it needs in
  `rrr.playback` and friends. Services the package itself runs are entry points
  (`rrr-api`). Run scripts as `uv run python scripts/<name>.py`, and never name
  one after a standard library module - see `docs/decisions.md` 31.
- Google-style docstrings, PEP 8, type hints.
- Commits: one purpose each, imperative one-line English message, no trailers.
  Never commit or push without being asked.

## The habit that matters most here

**Measure it, then write down what it cost.** Every decision in
`docs/decisions.md` names the alternatives and the measurement that settled it,
and `rrr.inspection` (run as `scripts/inspect_session.py`) re-reads a recording and makes the files argue with
each other rather than repeating what the recorder believed.

The other half of that habit is refusing to claim what has not been measured:
`session.json` leaves `calibration.offset_s` null and the page says "unmeasured"
rather than showing a zero nobody established. Extend that discipline to
anything new - an unmeasured extrinsic is null, not identity.

Two values are currently *asserted* rather than measured, and both should be
treated as unknown until something measures them:

- The microphone angles, 45/135/225/315 degrees in the chip's DOA convention.
  Only the page's level meter uses them (`MIC_LAYOUT` in
  `frontend/src/components/devices-panel.tsx`): the spacing is safe, the zero
  is a guess.
- The rigid transform between the camera and the array. `session.json` has a
  `rig` block for it, filled in by hand, and it ships `"unset"`. It is required
  before a direction estimate can become a ray in the world; an export carries
  it through verbatim and puts a note in the manifest rather than substituting
  an identity.

  It is expressed against the **depth** stream's frame, which is what every
  other transform in a recording is expressed against.

## What is built, and what is not (2026-09-07)

Built: the `rrr` namespace, the `rig` block, `events.jsonl` and its page
button, the full rig calibration (infrared intrinsics and baseline, inertial
extrinsics and the device's own correction), `RRR_EMITTER=on|off|alternating`,
`scripts/export.py`, and the page's Reconnect buttons. A failed device is never
retried automatically - see `docs/decisions.md` 29.

Not built, and deliberately so: pose. Monocular SLAM belongs outside this
repository - the export carries what it needs. This repository measures and
records; it does not estimate.

Untested by anything automatic: the adapters' real device I/O - `LiveSource`,
`Capture` and the USB direction readout. Everything above them, `SessionRecorder`
included, is tested against fakes of those three (`tests/conftest.py`).

Confirmed on the hardware (2026-09-07, firmware 5.17.3.10), with the numbers in
`docs/features.md` "What the calibration holds" and "The projector": depth is
computed in the left imager's frame, the inertial sensors share one frame, this
unit has **no IMU calibration** (it reads back as identity), and the emitter
modes work only through the sequence in `docs/decisions.md` 18.
