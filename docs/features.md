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
`ppx` 640.09 - which is the same thing config.py records about the depth output
being the infrared sensor cropped rather than scaled.

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
does this line up with the audio", but "how far apart were colour and depth
themselves":

| Field | What it means |
|---|---|
| `color_timestamp_ms` | the colour frame's own `frame.get_timestamp()`, or None if colour is disabled |
| `depth_timestamp_ms` | the depth frame's own, shared by both infrared frames - one imager, one exposure |
| `received_monotonic` | `time.monotonic()` when the set was assembled |

Neither of the first two is discarded, or a set with them, if they disagree
(decision 21): depth and infrared are validation data for a SLAM pipeline that
runs on colour, so a consumer reconciles the two after the fact rather than
have this repository guess at capture time which pairing to keep.

`session.json` holds a `(monotonic, realtime)` pair per second, so wall-clock
time is recoverable and an NTP step during the recording is visible rather than
smeared.

When `timestamp_domain` is `global_time` - librealsense's own device-to-host
clock fit, the default on Linux/RSUSB - `color_timestamp_ms` and
`depth_timestamp_ms` are also directly comparable to each other and to
`time.time() * 1000`, re-fitted while streaming (moved the mapping by about
10 ms over one minute of observation on a D455). Anything else - measured as
`system_time` on Windows - means each was stamped independently when its own
frame reached the SDK: see `docs/windows-native.md`.

## Honesty about losses

Every count is reported, and shown even when zero - a panel that hid them until
they went wrong would let a bad session look fine.

| Count | Means |
|---|---|
| `dropped` | the encoder queue was full: the disk or CPU fell behind, and the recording has holes |
| `skipped_duplicate` | a set the SDK re-delivered |
| `skipped_warmup` | discarded before the first good set, while the syncer settled. Two or three every time; not a loss |
| `timestamp_domain` | anything but `global_time` means colour and depth were stamped independently rather than through one drift-corrected clock - see `color_timestamp_ms` / `depth_timestamp_ms` (decision 21) |

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
- **Recording** also takes a mark: a label, and Enter or the button, written to
  `events.jsonl` with the time it landed on.
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

## The command line

Nothing here needs the server, and the server records through exactly this code.

```
make record SECONDS=30 SESSION=kitchen     # on the host: V4L2, loses frames
make drecord SECONDS=30                    # in the container: RSUSB, does not
make inspect DIR=data/sessions/kitchen      # cross-check a recording
make dserver                               # the page, on :8040
make export DIR=data/sessions/kitchen       # write it out as plain files
make check                                 # 229 tests, no device needed
```

`rrr.tools.inspect` is the one that matters after a recording. It re-reads the files
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
from rrr.video import ArchiveSource

with ArchiveSource("data/sessions/x/video.rrdb") as archive:
    for frames in archive.frames():
        frames.received_monotonic  # the common axis
        frames.depth               # (720, 1280) uint16, raw z16
        frames.color               # (800, 1280) uint16 YUYV
        frames.infrared            # (left, right), each (720, 1280) uint8
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

## Marks

Everything else in a session is a measurement a device made. `events.jsonl` is
the one sidecar written by a person: a label, stamped when the mark reached the
recorder, saying what was being done.

```json
{"monotonic": 1322248.12, "realtime": 1788250202.15,
 "label": "speaker 45deg 2m", "data": {"azimuth_deg": 45, "distance_m": 2.0}}
```

It exists because a recording of an experiment is unusable without knowing which
part of it was which condition, and "speaker at 45 degrees, two metres" is not
recoverable from the audio. `data` is free-form and this repository does not
interpret it: what belongs in it depends on the experiment, and fixing a schema
now would fix the wrong one.

**A mark is accurate to a person's reaction time, not to a sample.** Somebody
presses the button after they notice something, which is a few hundred
milliseconds late and varies. So a mark says what a *stretch* of a recording
was; when an instant has to be exact it comes from the signal - an onset in the
audio - and the mark only says what that onset was.

Marked from the page while recording (Enter in the field, or the button), and
the label stays after marking because a run is marked over and over with the
same condition. `make inspect` counts them and **fails if any of them falls
outside the recording**, which is how a sidecar from a different session gets
caught.

## Aligning the two devices

Both tracks share `CLOCK_MONOTONIC`, so any sample can be placed against any
frame. What is *not* known without measuring is the residual: how long a sound
takes to reach the array's converter against how long light takes to reach the
camera's shutter timestamp.

```
uv run python -m rrr.tools.calibrate data/sessions/<name>          # measure
uv run python -m rrr.tools.calibrate data/sessions/<name> --apply  # and record it
```

That measures *when*. **Where** is a separate question, and it is not measured
at all: `session.json` carries a `rig` block holding the transform from the
array's frame to the camera's, the microphone positions on the array, and which
channel of the WAV each microphone is. Every field starts empty.

```json
"rig": {
  "source": "unset",        // then "nominal" for design values, "measured" for this unit
  "rotation": null,         // row-major 3x3, camera_from_array
  "translation": null,      // metres, array origin in the camera frame
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
clap, ±16.7/√N for N. Until it has run, `calibration.offset_s` is null and the
page says "unmeasured" rather than showing zero.

## Exporting

`video.rrdb` is shaped for writing 54 MB/s without dropping anything, which is
the wrong shape for anything else to read. `rrr.tools.export` writes the same
recording as plain files - PNG images, CSV tables, a WAV - so a consumer needs a
filesystem and nothing else.

```bash
uv run python -m rrr.tools.export data/sessions/x -o /mnt/dataspace01/rrr
uv run python -m rrr.tools.export data/sessions/x --stride 5 --end 600
```

```
whole/
    manifest.json     the index: every stream, what it holds, where it is
    calibration.json  every sensor, every transform, and what is still unknown
    color/            index.csv + data/<sample>.png
    ir_left/          index.csv + data/<sample>.png
    ir_right/         index.csv + data/<sample>.png
    depth/            index.csv + data/<sample>.png   16-bit, raw z16
    frame_metadata/   index.jsonl
    imu_accel/        index.csv
    imu_gyro/         index.csv
    audio/            audio.wav + clock.csv + clock_fit.json
    doa/              index.csv
    events/           index.csv
    derived/          empty: where whatever is computed from this goes
```

Times are integer nanoseconds on `CLOCK_MONOTONIC`. Image indexes also retain
each sensor's own timestamp and its timestamp domain. Files are named with a
zero-padded sample id rather than a time: host clock values can repeat when two
frames arrive back-to-back, and a repeated time must not overwrite an image.
`frame_metadata/index.jsonl` keeps exposure, gain, laser power and other
variable firmware fields keyed by frame-set id. Streams are named for their role
rather than numbered, so removing a camera from the rig does not renumber the
others. Anything variable-length - eight microphones instead of four - is a
longer array in `calibration.json` rather than a change of layout.

One conversion happens, and the manifest names it: colour is written as RGB,
because nothing outside the SDK reads a packed YUYV image. The packed original
stays in the archive. Depth keeps its raw z16 and carries its scale, and the two
inertial streams stay apart rather than being resampled onto shared timestamps.

**The measured device offset is written down and not applied.** Applying it
would bake one alignment into files meant to outlast the decision. Where
something is unknown - an unset rig, an unmeasured offset - the export says so
in `notes` rather than substituting an identity.

`--start` and `--end` choose image-frame positions. Their half-open host-clock
interval is applied to audio, IMU, DOA and marks too; `--stride` only decimates
images. An export is written to a temporary directory, checked, and renamed into
place, so an interrupted conversion does not look complete. Check one again
without the source recording with:

```bash
uv run python -m rrr.tools.validate_export export/<session>
```

## MP4 review copies

The raw session remains the measurement, but a colour-and-sound review copy can
be made without exporting every stream first:

```bash
uv run python -m rrr.tools.render_mp4 data/sessions/walk-01 -o walk-01.mp4
```

The movie keeps the recorded frame timestamps, maps the WAV through
`audio.clock.jsonl`, and applies `calibration.offset_s` when it has been
measured. ReSpeaker's processed channel 0 is the default; `--audio-channel mix`
mixes the physical channels named by `rig.channels`, or nominal channels 1-4
when the rig is unset. A recorded DOA is drawn as a compass. With a complete
rig transform it is rotated into the colour-camera frame; otherwise it is
labelled as an unregistered array-frame angle. Missing calibration never
silently becomes an identity transform: without clock, offset, rig, or DOA the
tool falls back independently to a simple start-together audio/video movie.

Use `--no-doa` for a clean picture and `--force` to replace an existing movie.

## Not yet

- **A Raspberry Pi.** The image is built to be portable but has not run on one.
