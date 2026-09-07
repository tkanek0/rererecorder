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
| `../multimodal-spatial-awareness` | The analysis repository that consumes these recordings |
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
- **Monocular SLAM** is what the analysis side will implement. The infrared pair
  and depth are recorded as **reference data for validating monocular SLAM**, not
  as its input.
- **A neutral export format**, converted here, is how recordings reach the
  analysis repository. It does **not** import this package. `video.rrdb` is a
  performance-driven internal format and stays that way.
  `rrr.tools.export` writes it; the layout and its four principles are in
  `docs/decisions.md` 17.
- **Lab prototyping only.** No field site yet, so site-level anchoring, capacity
  profiles for long field sessions and redaction are all deferred.

## What the array can and cannot do

Worth knowing before designing any experiment on it, and worth stating in any
write-up. The ReSpeaker is measurably weaker than the Aria array the analysis
side has been working with:

| | Aria (AEA) | ReSpeaker USB Mic Array |
|---|---|---|
| widest baseline | 16.6 cm | 9.26 cm (opposite pair, 46.3 mm radius) |
| sample rate | 48 kHz | 16 kHz, fixed |
| max TDOA | 484 us = 23.2 samples | 270 us = 4.32 samples |
| alias-free limit, widest pair | 1034 Hz | 1852 Hz |
| one sample of TDOA, at broadside | about 2.5 deg | about **13.3 deg** |
| geometry | three-dimensional | **planar** - weak in elevation and front/back |

One consolation: the smaller aperture makes the far-field (plane wave)
approximation valid closer in, which is a problem the analysis side hit on Aria
with near-field sources.

The practical consequence is that **hand-measured ground truth is good enough**.
A source placed to 10 cm at 2 m is a 2.9 degree reference against a 13 degree
quantisation, so motion capture is not needed to measure this array.

## Conventions

- Everything lives under `rrr/`. Import as `from rrr.video import ArchiveSource`.
  Do not add top-level packages - see `docs/decisions.md` 15.
- Not a packaged project: no `[build-system]`, and both the Makefile and the
  container run from the checkout with the repository root on `PYTHONPATH`.
- Google-style docstrings, PEP 8, type hints.
- Commits: one purpose each, imperative one-line English message, no trailers.
  Never commit or push without being asked.

## The habit that matters most here

**Measure it, then write down what it cost.** Every decision in
`docs/decisions.md` names the alternatives and the measurement that settled it,
and `rrr/tools/inspect.py` re-reads a recording and makes the files argue with
each other rather than repeating what the recorder believed.

The other half of that habit is refusing to claim what has not been measured:
`session.json` leaves `calibration.offset_s` null and the page says "unmeasured"
rather than showing a zero nobody established. Extend that discipline to
anything new - an unmeasured extrinsic is null, not identity.

Two values are currently *asserted* rather than measured, and both should be
treated as unknown until something measures them:

- `rrr/audio/config.py` `MIC_ANGLES` - the file says "NOT YET VERIFIED", and
  nothing in the repository reads it.
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
and `rrr.tools.export`.

Not built, and deliberately so: pose. Monocular SLAM belongs to the analysis
repository - the export carries what it needs. This repository measures and
records; it does not estimate.

Untested by anything automatic: `SessionRecorder` itself, which needs a device.
That covers the mark sidecar's open-and-close lifecycle.

Confirmed on the hardware (2026-09-07, firmware 5.17.3.10):

- `depth_to_infrared[0]` is the identity, so depth really is computed in the
  left imager's frame. The stereo baseline is **95.13 mm**.
- The accelerometer and gyroscope report the same transform, so they are one
  frame - now checked rather than assumed.
- **This unit has no IMU calibration.** The correction reads back as the
  identity with zero bias, which matches the 9.69 m/s^2 gravity `inspect`
  already measured against a true 9.81. `rs-imu-calibration.py` writes one if
  it turns out to matter.
- All three emitter modes work, but only via the sequence in
  `docs/decisions.md` 18 - the obvious orderings are refused by the firmware,
  silently enough that a session would claim `alternating` while the projector
  stayed on.
