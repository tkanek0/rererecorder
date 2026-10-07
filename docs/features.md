# Features

What the recorder does today, and what it says about what it did.

## Recording

Every stream the D455 produces, at the resolution its sensors produce it. On
the machine it was built on that loses no frames; where a machine cannot keep
up, what was lost is counted ([Honesty about losses](#honesty-about-losses)).

| Stream | Recorded as | Codec | Size |
|---|---|---|---|
| depth 1280x720 z16 | `frames.depth` | zlib level 1 | 580 KB/frame |
| color 1280x800 YUYV | `frames.color_y` / `_u` / `_v` | PNG each | 761 KB/frame |
| IR left 1280x720 y8 | `frames.ir1` | PNG | 220 KB/frame |
| IR right 1280x720 y8 | `frames.ir2` | PNG | 226 KB/frame |
| accel 400 Hz | `imu` | plain columns | 48 B/sample |
| gyro 400 Hz | `imu` | plain columns | 48 B/sample |
| per-frame metadata | `frames.metadata` | JSON | 22 fields per stream |
| calibration, device, 48 sensor options | `meta` | JSON | written once |

**Everything is lossless.** Every codec was verified by decoding and comparing
against the original array, on real frames, not assumed from documentation.

Measured on a 34 second session: 1010 frames at 29.97 fps, `MISSING 0` on both
streams, frame interval 33.4 ms median with a 0.1 ms spread, 1.8 GB written at
53.9 MB/s.

Turn it down with `RRR_DEPTH`, `RRR_COLOR`, `RRR_INFRARED` or a `raw` codec when
a machine cannot keep up ([Configuration](#configuration)); see
[frame-loss.md](frame-loss.md) for what each resolution costs in field of view
and depth noise.

### What the calibration holds

Everything a consumer needs to relate one sensor to another, expressed against
**the depth stream's frame** - on a D400 that is the left infrared imager, and
it is what the SDK reports every other transform against.

| | |
|---|---|
| `color`, `depth`, `infrared` | intrinsics of each stream |
| `depth_to_color`, `depth_to_infrared` | where each imager sits |
| `infrared_baseline_m` | derived from the pair, and what fixes the scale of anything reconstructed from them |
| `motion.accel` / `.gyro` | the device's own correction: a 3x4 scale-and-misalignment matrix with a bias column, plus noise and bias variances |
| `motion.depth_to_accel` / `_gyro` | where the inertial sensor sits relative to the camera |

Measured on this D455 (firmware 5.17.3.10):

```
depth->ir_left    t=(0, 0, 0)                    identity     <- as a D400 should be
depth->ir_right   t=(-0.09513, 0, 0)             95.13 mm baseline
depth->color      t=(-0.05909, 0.00022, 0.00027)
depth->accel      t=(-0.03022, 0.0074, 0.01602)  } the same frame,
depth->gyro       t=(-0.03022, 0.0074, 0.01602)  } which is now checked rather than assumed
```

The infrared pair's intrinsics come back identical to depth's - `fx` 653.36,
`ppx` 640.09 - because the depth output is the infrared sensor cropped rather
than scaled ([frame-loss.md](frame-loss.md#what-the-sensors-actually-are)).

The inertial half matters for a moving rig and is not recoverable afterwards:
samples with no transform to the camera are numbers in an unnamed frame. **This
unit has no IMU correction**, though: it reads back as the identity with zero
bias, matching the 9.69 m/s^2 gravity `inspect` measures against a true 9.81.
Recorded anyway, because "the device says identity" and "no calibration was
recorded" have to stay distinguishable.

### The projector

`RRR_EMITTER` decides what the depth projector does, because the two things it
affects want opposite answers.

| Mode | Depth | Infrared |
|---|---|---|
| `on` (default) | best - the dots are what makes a blank wall matchable | **unusable for tracking**: the pattern is stuck to the scene, so a feature tracker follows the dots |
| `off` | degrades on untextured surfaces | clean |
| `alternating` | good on half the frames | clean on the other half |

Whichever is chosen, the projector's state is in **each frame's metadata** as
its laser power, so which frames were which is read from the recording rather
than assumed. Measured over 40 frames:

```
on            laser_power 150   mode 11111111111111111111111111111111
off           laser_power 0     mode 00000000000000000000000000000000
alternating   laser_power 0/150 mode 11110101010101010101010101010101
```

Alternating takes about four frames to settle, so the first few frames of a
recording are not yet toggling - another reason the per-frame metadata is what
to read rather than the mode that was asked for.

## Timing

Every frame carries `received_monotonic`: `time.monotonic()` when the set was
assembled - the one field every set is guaranteed to have, and the axis the
audio recording is also on. This is the number to compare against an audio
sample.

Also stored per frame, because they answer a different question - not "when
does this line up with the audio", but "how far apart were color and depth
themselves":

| Field | What it means |
|---|---|
| `color_timestamp_ms` | the color frame's own `frame.get_timestamp()`, or None if color is disabled |
| `depth_timestamp_ms` | the depth frame's own, shared by both infrared frames - one imager, one exposure |
| `received_monotonic` | `time.monotonic()` when the set was assembled |

Neither of the first two is discarded, or a set with them, if they disagree
(decision 21).

`session.json` holds a `(monotonic, realtime)` pair per second, so wall-clock
time is recoverable and an NTP step during the recording is visible rather than
smeared.

When `timestamp_domain` is `global_time` - librealsense's own device-to-host
clock fit, the default on Linux/RSUSB - `color_timestamp_ms` and
`depth_timestamp_ms` are also directly comparable to each other and to
`time.time() * 1000`, re-fitted while streaming (moved the mapping by about
10 ms over one minute of observation on a D455). `system_time`, which is what
Windows gives, means each was stamped independently when its own frame reached
the SDK: see `docs/windows-native.md`.

## Honesty about losses

Every count is reported, and shown even when zero - a panel that hid them until
they went wrong would let a bad session look fine.

| Count | Means |
|---|---|
| `dropped` | the encoder queue was full: the disk or CPU fell behind, and the recording has holes |
| `skipped_duplicate` | a set the SDK re-delivered |
| `skipped_warmup` | discarded before the first good set, while the syncer settled. Two or three every time; not a loss |
| `timestamp_domain` | anything but `global_time` means the sensors' own timestamps are not on one drift-corrected host clock; `system_time` means each stream was stamped on arrival, independently (decision 21) |

## The page

One page, served by vite on `:5177`, talking to the control plane (`rrr.api`)
on `:8040`. The page derives the control plane's host from its own location, so
it can be opened from another machine without configuring an address anywhere;
only the port is told, through `VITE_CONTROL_PORT`.

On Linux, `make up` starts both in containers, bound to `HOST` on `API_PORT` and
`APP_PORT`. Elsewhere they are started natively, one command each - see the
README.

- **Preview** - color and depth side by side, MJPEG at up to 15 Hz, 10 Hz
  while recording. Depth is shown
  next to color because the failure worth catching mid-recording is depth going
  blank while color looks perfect. Clicking one enlarges the same preview over
  the page; Escape, the backdrop or the close button closes it.
  Either can be turned off for this viewer alone - remembered in the browser,
  never sent to the server - which closes its stream and spares the encoding.
  Only a stream being captured, and so recorded, can be previewed.
- **The array** - each microphone's level, the chip's direction estimate
  (drawn in the same unmeasured convention as the microphones, so its zero is a
  guess), and whether the next recording records the array at all.
- **Recording** - start and stop, an optional session name, the counts above as
  they change, and [marks](#marks).
- **Storage** - free space and **how long that lasts**, computed from the rate
  the recording is actually achieving. Free bytes alone say little at 195 GB an
  hour: 200 GB reads as plenty and is one hour. The directory can be moved
  between sessions, not during one.
- **Sessions** - every recording, newest first, with its losses. Play or delete.
  Play is disabled while recording, and playing hides the device, recording and
  storage panels, leaving only the player and this list until it is closed.
- **Playback** - play, pause, step, seek, four video streams, six audio
  channels, 0.25x to 4x. The clock shown is each frame's own recorded time,
  read from a response header, because frames are not evenly spaced.

  With audio, **the audio element is the clock**: each animation frame asks
  which video frame belongs to its current position, and frames that cannot be
  fetched in time are skipped rather than queued. A stutter in the sound is
  audible; a late video frame is not. Without audio, the frames drive
  themselves at the recorded rate.

Playing pauses when the tab is hidden: Chrome throttles a background tab's
timers to the point where a `setTimeout(10)` took 557 ms, so the loop would crawl
while the button still said Pause.

**A failed device stays failed** (decision 29). When the camera or the array
cannot be opened, or stops delivering, its card shows why and a **Reconnect**
button, which also enumerates the device again. It is disabled while recording.

## The command line

Nothing here needs the server, and the server records through exactly this code.
Each is a script in `scripts/`, listed in [scripts/README.md](../scripts/README.md).

```
uv run python scripts/record.py --seconds 30 --session kitchen     # record
uv run python scripts/inspect_session.py data/sessions/kitchen     # cross-check a recording
uv run python scripts/export.py data/sessions/kitchen             # write it out as plain files
uv run python scripts/render_mp4.py data/sessions/kitchen         # a review movie
uv run pytest                                                      # no device needed
```

On Linux, record through the container instead (decision 30); the Makefile's
`COMPOSE` line shows the variables it needs:

```
docker compose run --rm api python scripts/record.py --seconds 30
```

`scripts/inspect_session.py` is the one that matters after a recording. It
re-reads the files and makes them argue with each other rather than summarising
what the recorder believed; the checks themselves are `rrr.inspection`:

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

Frames lost before the recorder are counted from the camera's own
`frame_counter`, per stream, the way [frame-loss.md](frame-loss.md) counted them:
a gap is a frame the camera produced and never delivered, and a counter that
goes back to zero is a stream that restarted mid-recording. Both fail the
check. Without UVC metadata the counter is the host's own and proves nothing,
so the losses are reported as not counted rather than as zero.

What the recorder itself knew went wrong is not left to the files to reveal:
every entry in `session.json`'s `errors` fails the check, as does a session
with neither device or a direction track with no readings.

## Reading a recording

```python
from rrr.video import ArchiveSource

with ArchiveSource("data/sessions/x/video.rrdb") as archive:
    for frames in archive.frames():
        frames.received_monotonic  # the common axis
        frames.depth  # (720, 1280) uint16, raw z16
        frames.color  # (800, 1280) uint16 YUYV
        frames.infrared  # (left, right), each (720, 1280) uint8
```

Or without the SDK, since the container is SQLite:

```sql
SELECT idx, received_monotonic, length(depth) FROM frames ORDER BY idx;
```

For one frame out of the middle, `archive.frame_at(index, only="color")` decodes
that stream alone - 11 ms against 30 ms for all of them.

## The array

Recorded beside the camera, in the same session and on the same clock.

| Stream | Recorded as | Notes |
|---|---|---|
| 6 channels, 16 kHz | `audio.wav` | int16, every channel - the raw microphones cannot be recovered from the processed one |
| capture times | `audio.clock.jsonl` | one measured point per second, plus every gap |
| direction | `doa.jsonl` | 15 Hz, the chip's own estimate |

Audio has no timestamps of its own, so the sidecar carries them. Without it a
WAV can only be read through `frames / rate`, and that assumption fails twice:
the converter does not run at exactly 16 kHz (measured -8 to -42 ppm on this
unit), and a dropped sample would shift everything after it. Gaps are filled
with silence so that a file position keeps meaning a time, and the fill is
recorded so the repair can be checked.

The array has been seen to stop delivering audio mid-recording while its PCM
still reads `RUNNING`, with nothing in the kernel log; what triggers it is not
known. Two seconds without a block count as a failure: the recording carries on
with the camera, `session.json` names the error, and the page's Reconnect opens
the array again once the recording is stopped. A failed direction readout is
named in `errors` the same way, and so is anything else a device reports.

## Marks

Everything else in a session is a measurement a device made. `events.jsonl` is
the one sidecar written by a person: a label, stamped when the mark reached the
recorder, saying what was being done.

```json
{"monotonic": 1322248.12, "realtime": 1788250202.15,
 "label": "speaker 45deg 2m", "data": {"azimuth_deg": 45, "distance_m": 2.0}}
```

`data` is free-form and this repository does not interpret it. **A mark is
accurate to a person's reaction time, not to a sample**: it says what a stretch
of a recording was, never when something happened (decision 16).

Marked from the page while recording (Enter in the field, or the button), and
the label stays after marking because a run is marked over and over with the
same condition. `rrr.inspection` counts them and **fails if any of them falls
outside the recording**, which is how a sidecar from a different session gets
caught.

## Aligning the two devices

Both tracks share `CLOCK_MONOTONIC`, so any sample can be placed against any
frame. What is *not* known without measuring is the residual: how long a sound
takes to reach the array's converter against how long light takes to reach the
camera's shutter timestamp.

```
uv run python scripts/calibrate.py data/sessions/<name>          # measure
uv run python scripts/calibrate.py data/sessions/<name> --apply  # and record it
```

That measures *when*. **Where** is a separate question, and it is not measured
at all: `session.json` carries a `rig` block holding the transform from the
array's frame to the depth stream's (the frame every other transform in a
recording is expressed against), the microphone positions on the array, and which
channel of the WAV each microphone is. Every field starts empty.

```json
"rig": {
  "source": "unset",        // then "nominal" for design values, "measured" for this unit
  "rotation": null,         // row-major 3x3, depth_from_array
  "translation": null,      // metres, array origin in the depth stream's frame
  "microphones": null,      // metres, in the array frame
  "channels": null,         // which WAV channel each microphone is
  "description": null,
  "note": null
}
```

It is meant to be edited into the file by hand, which is why it is a declared
field rather than something a consumer bolts on: an unknown key would be dropped
the first time anything rewrote the session, and `calibrate --apply` does. A
half-filled block reads as unknown rather than as half a mounting, and a
malformed field costs that field rather than the session.

Clap a few times in front of the camera, close to the array. The tool finds the
impulse in the audio (sub-millisecond) and the peak frame-to-frame difference in
the video (one frame), so **the frame rate bounds the answer**: ±16.7 ms for one
clap, ±16.7/√N for N. Until it has run, `calibration.offset_s` is null.

## Exporting

`scripts/export.py` writes a recording as plain files - PNG images, CSV tables, a
WAV - so a consumer needs a filesystem and nothing else (decision 17).

```bash
uv run python scripts/export.py data/sessions/x                  # into data/sessions/x/export
uv run python scripts/export.py data/sessions/x -o /mnt/other/x  # exactly there
uv run python scripts/export.py data/sessions/x --stride 5 --end 600
```

By default the export goes inside the session, so a recording and what was made
from it are kept, moved and deleted together. `-o` names the export directory
itself, not a parent to put one in.

The layout - every file, column and key, and what a reader may rely on - is in
[export-format.md](export-format.md). In short: one directory per role
(`color`, `ir_left`, `depth`, `imu_accel`, `audio`, ...) indexed by
`manifest.json`, times in integer nanoseconds on `CLOCK_MONOTONIC`, color as
RGB and depth as raw z16, and the measured device offset written down but not
applied.

`--start` and `--end` choose image-frame positions. Their half-open host-clock
interval is applied to audio, IMU, DOA and marks too; `--stride` only decimates
images. Check an export again without the source recording with:

```bash
uv run python scripts/validate_export.py data/sessions/<session>/export
```

## MP4 review copies

The raw session remains the measurement, but a color-and-sound review copy can
be made without exporting every stream first:

```bash
uv run python scripts/render_mp4.py data/sessions/walk-01   # data/sessions/walk-01/review.mp4
```

The movie keeps the recorded frame timestamps, maps the WAV through
`audio.clock.jsonl`, and applies `calibration.offset_s` when it has been
measured. ReSpeaker's processed channel 0 is the default; `--audio-channel mix`
mixes the physical channels named by `rig.channels`, or nominal channels 1-4
when the rig is unset. A recorded DOA is drawn as a compass. With a complete
rig transform it is rotated into the color-camera frame; otherwise it is
labelled as an unregistered array-frame angle. Missing calibration never
silently becomes an identity transform: without clock, offset, rig, or DOA the
tool falls back independently to a simple start-together audio/video movie.

Use `--no-doa` for a clean picture, `--force` to replace an existing movie, and
`-o` to write it somewhere other than `review.mp4` inside the session.

A GIF is the same presentation copy in a form that goes into a slide or a
message:

```bash
uv run python scripts/render_gif.py data/sessions/walk-01                         # ./video.gif
uv run python scripts/render_gif.py data/sessions/walk-01 --volume --waveform -o walk-01.gif
```

Every `--stride`-th color frame (default 15) is kept, scaled to `--width`, and
given its own palette. `--volume` adds the whole recording's loudness (linear
RMS, scaled to its loudest column) with a playhead; `--waveform` adds the
`--window-s` seconds ending at each frame on one amplitude scale. Both place the
WAV on the video clock exactly as the MP4 does, and both default to the mix of
the physical microphones rather than the processed channel, which the array has
already beamformed and gain-controlled for speech recognition. Asking for either
without a WAV is an error, not a silent omission.

## Configuration

Everything is read from the environment once, at start; the page can change the
streams, codecs, directory and whether the array is recorded between recordings.
Flags take `1`/`0`. In the container, `compose.yaml` passes each of these
through when it is set.

| Variable | Default | What it sets |
|---|---|---|
| `RRR_SESSIONS_DIR` | `data/sessions` | where sessions are created ([decisions](decisions.md) 28) |
| `RRR_DEPTH`, `RRR_COLOR` | `1280x720@30`, `1280x800@30` | a stream's `WIDTHxHEIGHT@FPS`, or `off` |
| `RRR_INFRARED`, `RRR_MOTION` | `1`, `1` | the infrared pair, the inertial sensor |
| `RRR_COLOR_FORMAT` | `yuyv` | `yuyv` or `rgb8` (decision 4) |
| `RRR_ALIGN` | `0` | resample depth into the color camera (decision 2) |
| `RRR_EMITTER` | `on` | `on`, `off` or `alternating` ([The projector](#the-projector)) |
| `RRR_DEPTH_CODEC`, `RRR_COLOR_CODEC`, `RRR_INFRARED_CODEC` | `compressed` | `compressed` or `raw` (decision 22) |
| `RRR_SERIAL` | first found | which camera to open |
| `RRR_VIDEO`, `RRR_AUDIO`, `RRR_DOA` | `1` | whether each is recorded at all; the page can change the audio's. The live views work either way |
| `RRR_AUDIO_DEVICE` | `ReSpeaker` | part of the array's name as PortAudio lists it |
| `RRR_AUDIO_BLOCK_SIZE`, `RRR_AUDIO_WINDOW_S` | `256`, `10` | samples per capture block; seconds kept in memory |
| `RRR_AUDIO_DOA_POLL_HZ` | `15` | how often the direction is read |
| `RRR_IDLE_SHUTDOWN_S` | `20` | how long a device stays open after its last user leaves |
| `RRR_API_HOST`, `RRR_API_PORT` | `0.0.0.0`, `8040` | where the control plane listens |
| `RRR_PREVIEW_WIDTH`, `RRR_JPEG_QUALITY` | `640`, `80` | the live preview's size and quality |
| `RRR_PREVIEW_MAX_HZ_IDLE`, `RRR_PREVIEW_MAX_HZ_RECORDING` | `15`, `10` | the live preview's rate |
| `RRR_AUDIO_LEVEL_HZ`, `RRR_AUDIO_LEVEL_WINDOW_S` | `10`, `0.1` | the level meter's rate and window |
| `RRR_ALLOW_ORIGINS` | `*` | origins the page may be served from |
| `RRR_ALLOW_SETTINGS_WRITE` | `1` | whether the page may change settings |
| `RRR_SHUTDOWN_TIMEOUT_S` | `5` | how long the server waits for open streams when stopping |
| `VITE_CONTROL_PORT` | `8040` | the port the page looks for the control plane on |

`make up` and `docker compose` also read `HOST`, `API_PORT` and `APP_PORT`
(Makefile) and `RRR_DATA` and `RRR_CPUS` (`compose.yaml`).

## Not yet

- **The offset between the two devices is unmeasured** until `scripts/calibrate.py`
  runs on a session (see [Aligning the two devices](#aligning-the-two-devices)).
- **A Raspberry Pi.** The image is built to be portable but has not run on one,
  and lossless at 54 MB/s will not fit there.
- **Windows drops frames** with more than color alone, and Media Foundation
  stamps color and depth independently. What is lost is recorded in the
  session like anywhere else ([windows-native.md](windows-native.md)).
