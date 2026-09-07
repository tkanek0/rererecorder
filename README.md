# rererecorder

Recording an Intel RealSense D455 - and, next, a ReSpeaker USB Mic Array beside
it - so that the two can be lined up afterwards, exactly, from the files.

```
session 2026-09-02_15-28-36
  video           1010 frames over 33.67 s = 29.97 fps
  frame interval  33.4 ms median, 33.4 min, 33.5 max
  depth  1010 frames, MISSING 0
  color  1010 frames, MISSING 0
```

Depth at 1280x720, colour at 1280x800, both raw infrared images, all at 30 fps,
all lossless, with nothing dropped - plus the inertial sensor at its own 480 Hz
rather than sampled once per frame. 54 MB/s, or 195 GB an hour.

## Recording needs the container

The `pyrealsense2` wheel on PyPI is built against V4L2, and V4L2 loses **8.4% of
depth frames** on this camera. The image here compiles librealsense with the
RSUSB backend instead, which loses none - the investigation is in
[docs/frame-loss.md](docs/frame-loss.md).

```bash
make image                      # build it (compiles librealsense, ~4 min)
make dserver                    # the page, on http://localhost:8040
make drecord SECONDS=30         # or just record, from the terminal
```

`make help` lists the rest. Running on the host works and is fine for developing
the page, but it will drop frames.

## What a session is

```
var/sessions/2026-09-02_15-28-36/
    session.json        clock anchors, calibration and rig state, what went wrong
    video.rrdb          SQLite: frames, motion, calibration, sensor options
    audio.wav           every channel, gaps filled with silence
    audio.clock.jsonl   measured capture time per block
    doa.jsonl           the array's direction estimate
    events.jsonl        marks made by hand, saying what was happening
```

Every frame carries `capture_monotonic` - the camera's own idea of when it
happened, on the one axis everything else uses. Check a recording with:

```bash
make inspect DIR=var/sessions/2026-09-02_15-28-36
```

which re-reads the files and makes them argue with each other, rather than
repeating what the recorder believed.

## Reading one back

```python
from rrr.video import ArchiveSource

with ArchiveSource("var/sessions/x/video.rrdb") as archive:
    for frames in archive.frames():
        frames.capture_monotonic   # the common axis
        frames.depth               # (720, 1280) uint16, raw z16
        frames.color               # (800, 1280) uint16 YUYV
        frames.infrared            # (left, right), each (720, 1280) uint8
```

No SDK needed if the question is about the file rather than the pictures - the
container is SQLite.

## Documentation

| | |
|---|---|
| [design.md](docs/design.md) | the one idea, module boundaries, why the camera is shared two ways |
| [decisions.md](docs/decisions.md) | seventeen choices, the alternatives, and the measurement that decided each |
| [features.md](docs/features.md) | what it records, what it reports, and how to drive it |
| [frame-loss.md](docs/frame-loss.md) | the frame-loss investigation: seven wrong hypotheses and the right one |

Seventeen decisions, nearly all of them settled by a measurement rather than
by taste.

## State

Both devices record together, on one clock, cross-checked. What is left:

- **The offset between them is unmeasured.** They share a clock, but the residual
  between a microphone and a shutter has to be measured from a handclap -
  `rrr/tools/calibrate.py`. Until it runs, `session.json` says "unmeasured" rather
  than claiming zero.
- **A Raspberry Pi** has not run this. Lossless at 54 MB/s will not fit there.

Tests: `make check` - 152 of them, none needing a device.
