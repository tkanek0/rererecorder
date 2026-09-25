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

Every frame carries `received_monotonic` - the one axis everything else uses -
plus `color_timestamp_ms` and `depth_timestamp_ms`, each sensor's own idea of
when it happened. Check a recording with:

```bash
make inspect DIR=var/sessions/2026-09-02_15-28-36
```

which re-reads the files and makes them argue with each other, rather than
repeating what the recorder believed.

Make an ordinary H.264/AAC review movie directly from the colour camera and
ReSpeaker recording with:

```bash
uv run python -m rrr.tools.render_mp4 var/sessions/<name> -o <name>.mp4
```

Recorded clocks and calibration are applied when available; unset calibration
falls back to a simple start-together movie without claiming a correction.

## Reading one back

```python
from rrr.video import ArchiveSource

with ArchiveSource("var/sessions/x/video.rrdb") as archive:
    for frames in archive.frames():
        frames.received_monotonic  # the common axis
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
| [decisions.md](docs/decisions.md) | twenty-three choices, the alternatives, and the measurement that decided each |
| [features.md](docs/features.md) | what it records, what it reports, and how to drive it |
| [frame-loss.md](docs/frame-loss.md) | the frame-loss investigation: seven wrong hypotheses and the right one |
| [windows-native.md](docs/windows-native.md) | recording on Windows, natively and under WSL2: what's fixed, what's still open |

Twenty-two decisions, nearly all of them settled by a measurement rather than by
taste.

## State

Both devices record together, on one clock, cross-checked. What is left:

- **The offset between them is unmeasured.** They share a clock, but the residual
  between a microphone and a shutter has to be measured from a handclap -
  `src/rrr/tools/calibrate.py`. Until it runs, `session.json` says "unmeasured" rather
  than claiming zero.
- **A Raspberry Pi** has not run this. Lossless at 54 MB/s will not fit there.
- **Native Windows records every frame, with no fixed cross-sensor sync.**
  Media Foundation does not correct colour and depth onto one clock, so each
  is timestamped independently rather than discarded when they disagree.
  **Operate with colour alone and the raw codec on this platform** - the one
  combination measured to hold a lossless 30 fps with zero drops, confirmed
  over both a 10-minute CLI recording and a 73-minute run through the server
  with a live preview attached. Adding depth and/or infrared costs more than
  encoding alone predicts, for reasons not fully isolated - see
  [windows-native.md](docs/windows-native.md). Which streams are captured and
  how each is stored (`compressed` or `raw`) can both be chosen from the CLI
  (`--no-depth`, `--color-codec`, ...) or the page's settings panel, rather
  than only through `RRR_*` environment variables - decision 23.

Tests: `make check` - 252 of them, none needing a device.
