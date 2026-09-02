# Features

What the recorder does today, and what it says about what it did.

## Recording

Every stream the D455 produces, at the resolution its sensors produce it, with
no frames lost.

| Stream | Recorded as | Codec | Size |
|---|---|---|---|
| depth 1280x720 z16 | `frames.depth` | zlib level 1 | 580 KB/frame |
| colour 1280x800 YUYV | `frames.color_y` / `_u` / `_v` | PNG each | 761 KB/frame |
| IR left 1280x720 y8 | `frames.ir1` | PNG | 220 KB/frame |
| IR right 1280x720 y8 | `frames.ir2` | PNG | 226 KB/frame |
| accel 482 Hz | `imu` | plain columns | 48 B/sample |
| gyro 478 Hz | `imu` | plain columns | 48 B/sample |
| per-frame metadata | `frames.metadata` | JSON | 22 fields per stream |
| calibration, device, 48 sensor options | `meta` | JSON | written once |

**Everything is lossless.** Every codec was verified by decoding and comparing
against the original array, on real frames, not assumed from documentation.

Measured on a 34 second session: 1010 frames at 29.97 fps, `MISSING 0` on both
streams, frame interval 33.4 ms median with a 0.1 ms spread, 1.8 GB written at
53.9 MB/s.

Turn it down with `RRR_DEPTH`, `RRR_COLOR`, `RRR_INFRARED` when a machine cannot
keep up - see [frame-loss.md](frame-loss.md) for what each resolution costs in
field of view and depth noise.

## Timing

Every frame carries `capture_monotonic`: the camera's own idea of when the frame
happened, converted onto `CLOCK_MONOTONIC` using the clock offset measured for
that frame. This is the number to compare against an audio sample.

Also stored per frame, because they answer different questions:

| Field | What it means |
|---|---|
| `timestamp_ms` | epoch milliseconds as the SDK reports them (`global_time`) |
| `received_at` | when `wait_for_frames` returned - 16.8 ms later, measured |
| `capture_monotonic` | `timestamp_ms` on the common axis |

`session.json` holds a `(monotonic, realtime)` pair per second, so wall-clock
time is recoverable and an NTP step during the recording is visible rather than
smeared.

The accuracy limit is the camera's, not this code's: librealsense re-fits its
global-time estimate while streaming, which moved the mapping by about 10 ms over
one minute of observation.

## Honesty about losses

Every count is reported, and shown even when zero - a panel that hid them until
they went wrong would let a bad session look fine.

| Count | Means |
|---|---|
| `dropped` | the encoder queue was full: the disk or CPU fell behind, and the recording has holes |
| `skipped_unpaired` | a set whose streams disagreed by more than 5 ms - discarded, since it is not one moment |
| `skipped_duplicate` | a set the SDK re-delivered |
| `skipped_warmup` | discarded before the first good set, while the syncer settled. Two or three every time; not a loss |
| `timestamp_domain` | anything but `global_time` means the frames cannot be placed against audio |

## The page

One page at `:8040`, served by the same process that records.

- **Preview** - colour and depth side by side, MJPEG at 10 fps. Depth is shown
  next to colour because the failure worth catching mid-recording is depth going
  blank while colour looks perfect.
- **Recording** - start and stop, an optional session name, and the counts above
  as they change.
- **Storage** - free space and **how long that lasts**, computed from the rate
  the recording is actually achieving. Free bytes alone say little at 195 GB an
  hour: 200 GB reads as plenty and is one hour. The directory can be moved
  between sessions, not during one.
- **Sessions** - every recording, newest first, with its losses. Play or delete.
- **Playback** - play, pause, step, seek, four streams, 0.25x to 4x. The clock
  shown is each frame's own recorded time, read from a response header, because
  frames are not evenly spaced.

Playing pauses when the tab is hidden: Chrome throttles a background tab's
timers to the point where a `setTimeout(10)` took 557 ms, so the loop would crawl
while the button still said Pause.

## The command line

Nothing here needs the server, and the server records through exactly this code.

```
make record SECONDS=30 SESSION=kitchen     # on the host: V4L2, loses frames
make drecord SECONDS=30                    # in the container: RSUSB, does not
make inspect DIR=var/sessions/kitchen      # cross-check a recording
make dserver                               # the page, on :8040
make check                                 # 152 tests, no device needed
```

`tools.inspect` is the one that matters after a recording. It re-reads the files
and makes them argue with each other rather than summarising what the recorder
believed:

```
  video           1010 frames over 33.67 s = 29.97 fps
  frame interval  33.4 ms median, 33.4 min, 33.5 max
  arrival lag     16.8 ms median (12.7 to 22.5)
  conversion      agrees with the clock samples to 0.006 ms
  length          45.056 s by header, 45.056 s by clock points (0.5 ms apart)
  inertial        8515 samples (accel 399 Hz, gyro 399 Hz)
  gravity         9.69 m/s^2 median magnitude (9.81 if still)
  audio clock     16000.17 Hz fitted (+11 ppm) from 46 points
  residual        0.017 ms rms, 0.043 ms max
  every cross-check agreed
```

The gravity line is the one check here that comes from physics rather than from
the file agreeing with itself: a stationary accelerometer measures specific
force, so its magnitude should be gravity. This camera reads 9.69, matching
realsense-playground's independent measurement of the same unit.

The two length figures come from different numbers - the WAV header's
`frames / rate`, and a line fitted to measured clock points - so their agreeing
means something. Its exit status is 1 if any check disagreed.

## Reading a recording

```python
from video import ArchiveSource

with ArchiveSource("var/sessions/x/video.rrdb") as archive:
    for frames in archive.frames():
        frames.capture_monotonic   # the common axis
        frames.depth               # (720, 1280) uint16, raw z16
        frames.color               # (800, 1280) uint16 YUYV
        frames.infrared            # (left, right), each (720, 1280) uint8
```

Or without the SDK, since the container is SQLite:

```sql
SELECT idx, capture_monotonic, length(depth) FROM frames ORDER BY idx;
```

For one frame out of the middle, `archive.frame_at(index, only="color")` decodes
that stream alone - 11 ms against 30 ms for all of them.

## Not yet

- **The ReSpeaker.** The audio path is built and tested (its clock work is the
  more delicate half) but is not wired into the container, and the offset between
  the two devices is unmeasured.
- **A Raspberry Pi.** The image is built to be portable but has not run on one.
