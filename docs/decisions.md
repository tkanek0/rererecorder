# Decisions

What was chosen, what else was considered, and the measurement that decided it.

---

## 1. Build librealsense with the RSUSB backend

**Chosen:** compile librealsense with `-DFORCE_RSUSB_BACKEND=true` and its
Python bindings, inside a container image.

**Alternatives:** the `pyrealsense2` wheel from PyPI (V4L2); lowering the
resolution until V4L2 keeps up.

**Why:** the wheel loses 8.4% of depth frames and 9.6% of colour frames at
1280x720 + 1280x800. The same test through RSUSB loses none, at 172 MB/s with
both raw infrared streams added. The full investigation - and the seven
hypotheses that turned out wrong - is in [frame-loss.md](frame-loss.md).

**Cost:** the wheel cannot be used, so recording needs the image. `uv sync
--no-install-package pyrealsense2` excludes it, and the image fails at build
time if the import does not work, because a silent fallback to V4L2 would look
like a working recorder that drops 8% of its frames.

---

## 2. Record without aligning depth to colour

**Chosen:** `align_to_color=False`. Store `depth_to_color` instead.

**Alternatives:** align, as realsense-playground does; store both.

**Why:** alignment resamples depth onto the colour camera's 1280x800 grid. That
cannot be undone, it destroys the pixel correspondence with the infrared pair -
which is the reason for keeping the pair at all - and it bakes one choice into a
file meant to outlast it. Every consumer can align on the way out; none can
un-align.

**Cost:** `depth[y, x]` does not describe `color[y, x]`. Anything wanting that
has to apply the extrinsics itself.

---

## 3. Keep the raw infrared pair

**Chosen:** record IR 1 and IR 2 alongside depth.

**Alternatives:** depth and colour only (40 MB/s instead of 54).

**Why:** the depth in a recording is one particular stereo match, made by this
camera's ASIC with this firmware. The infrared pair is what it was made from, so
keeping it means the depth can be recomputed by a different matcher later. It is
the difference between recording a conclusion and recording the evidence.

**Cost:** 27 MB/s compressed, a third of the session's size. IR can only be
opened at the depth stream's own resolution, so 1280x800 IR and depth cannot be
had at once.

---

## 4. Store colour as YUYV in three planes

**Chosen:** ask the sensor for YUYV and store Y, U and V as three PNGs.

**Alternatives:** convert to RGB (what the SDK does if asked); store the packed
YUYV buffer as one 16-bit PNG; JPEG.

**Why:** YUYV is what the sensor emits - converting to RGB costs CPU and 50%
more bytes without adding information, since the chroma is already subsampled.
Storing it packed compresses badly, because Y and U alternate byte by byte and a
predictor has nothing to work with. Measured on real frames:

| Form | ms | KB | Lossless |
|---|---|---|---|
| Packed, PNG16 | 30.8 | 870 | yes |
| **Three planes, PNG** | **27.9** | **761** | **yes** |
| Packed, zlib | 22.4 | 1161 | yes |

The split and its inverse were checked byte-for-byte on every frame, not
assumed.

**Cost:** three columns instead of one, and a reader has to reassemble them.
`rrr.video.types.join_yuyv` does, and `rrr.server.preview.to_bgr_from_planes` skips the
reassembly for display.

---

## 5. Compress depth with zlib, not PNG16

**Chosen:** `zlib` level 1, with `png16` available.

**Why:** measured on 1280x720 depth from this camera, zlib is both faster and
smaller - 12.2 ms and 580 KB against 24.1 ms and 646 KB. PNG's row predictors
work on bytes, and a 16-bit depth image interleaves high and low bytes.

**Cost:** the blob is values and nothing else, so the shape must come from the
archive's calibration. Without one the reader refuses rather than reshaping to
whatever fits - a plausible-looking image of the wrong dimensions is worse than
an error.

---

## 6. A directory per session, not one container

**Chosen:** `session.json` + `video.rrdb` + `audio.wav` + two sidecars.

**Alternatives:** everything in one SQLite file.

**Why:** a single container would mean the audio could not be opened by anything
that opens a WAV, and two devices would contend for one writer. The manifest
carries what a directory of separate files cannot say for itself: which clock,
which offsets, what failed.

**Cost:** five files to keep together. `SessionPaths` fixes their names so a
directory can be understood without reading the manifest first.

---

## 7. Two ways out of the frame hub

**Chosen:** `latest()` for the preview, `add_listener()` for recording.

**Alternatives:** poll `latest(after=…)` for both, as realsense-playground's
recorder does; give each consumer a queue.

**Why:** `latest` returns the newest set, so a recorder reading it loses a frame
whenever it is briefly late - and cannot tell that it did. Recording is what
this repository is for. A listener runs on the hub's thread for every set, in
order, so nothing is dropped.

**Cost:** a listener must be quick, since the next frame cannot be published
until it returns. `append` is a queue put; the encoding happens on the archive's
own pool.

---

## 8. `format_version 2` and the `.rrdb` suffix

**Chosen:** bump the version, change the suffix, keep reading v1.

**Why:** v2 splits colour across three columns and may store depth as zlib, so
the meaning of existing columns changed. A v1 reader opening one would misread
it - better that it refuses, which realsense-playground does for any version but
its own. Keeping `.rsdb` would have invited exactly that mistake.

Note the contrast with `capture_monotonic`, which was **added** without moving
the version: every reader selects columns by name, so an older one ignores it and
reads the rest correctly. Bumping there would have broken compatibility in both
directions to describe a change that harms neither.

---

## 9. Frontend: vite + react, not a single HTML file

**Chosen:** `web/`, built in a Docker stage, served as static files.

**Alternatives:** one hand-written HTML file with no build step (chosen first,
then reversed on request).

**Why:** it matches both playground repositories, and the runtime image needs no
node - what ships is a few hundred kilobytes of static files, so a Raspberry Pi
never compiles anything.

**Cost:** a build step, and `npm ci` in the image build.

---

## 10. Playback fetches frames one at a time

**Chosen:** `GET /api/sessions/{id}/frame/{index}.jpg`, driven by JavaScript.

**Alternatives:** MJPEG, like the live preview.

**Why:** MJPEG cannot seek and cannot say which frame is on screen. Measured,
the cost of a request is 24 ms server-side and 24 ms end-to-end in the browser,
so 30 fps has room. Each response carries `X-Capture-Monotonic`, so the clock
shown is the recording's own - frames are not evenly spaced, because a mispaired
set leaves a gap.

**Cost:** a request per frame. The archive is opened per request too: measured at
1.3 ms, against 16 ms of decoding, so caching one open archive would save
nothing and would need a lock.

---

## 11. Store sessions on the SATA SSD

**Chosen:** `/mnt/dataspace02/rererecorder` by default in the container, with
the directory changeable from the page.

**Why:** measured through the recorder's own access pattern - BLOBs in
transactions with a WAL - the SATA drive sustains 198 MB/s against the 54 MB/s a
recording needs, and it has 1.6 TB free (about 8 hours). The NVMe is faster in a
burst (290 MB/s) but **falls off after 3 GB** as its SLC cache fills, from 364
to 242 MB/s, while the SATA drive held 195-207 MB/s for 6 GB without wavering.
For long recordings the slower drive is the steadier one.

---

## 12. Open the inertial sensor separately, at its own rate

**Chosen:** open the motion module directly with `sensor.start(callback)`,
outside the video pipeline, and record every sample it delivers.

**Alternatives:** take it from the frameset, as realsense-playground does (one
accelerometer and one gyroscope reading per video frame); use
`rs.frame_queue` instead of a callback.

**Why:** through the frameset a recording held exactly one sample per frame -
measured, 1010 motion rows for 1010 frames, 33.4 ms apart = 30.0 Hz - while the
sensor runs at 482 Hz accelerometer and 478 Hz gyroscope. That discarded 93% of
what the IMU measured, which contradicts the position taken in decision 3 about
keeping the evidence rather than the conclusion.

The callback was chosen over a frame queue on measurement. Both keep the video
intact, but the queue lost inertial samples:

| Mode | depth lost | colour lost | video sets | Accel interval, max |
|---|---|---|---|---|
| no IMU | 0.0% | 0.0% | 28.92/s | - |
| **callback** | **0.0%** | **0.0%** | **30.17/s** | **2.5 ms** |
| frame_queue | 0.0% | 0.0% | 28.83/s | **70.8 ms** |

The GIL contention a 960 Hz Python callback looks like it should cause does not
materialise, because the callback only appends a tuple. (The first attempt at
this measurement was run on the host and showed 12% loss in *all three* modes,
including with no IMU at all - that was the V4L2 backend, not the IMU. Decision
1 again.)

**Cost:** the `motion` table is replaced by `imu`, which moves the format to
version 3, and about 30 KB/s - 0.06% of the video. The samples start up to 0.7 s
before the first frame and have gaps while the sensor settles; `rrr/tools/inspect`
looks for gaps only inside the video's own span for that reason.

`FrameSet.motion` still exists, holding the newest buffered sample of each
stream, because a preview or a quick attitude estimate wants one number per
frame. It is not written to the archive - storing a fourteenth of the data
twice - and its docstring says what it is.

---

## 13. Both devices in one container, with the audio group granted

**Chosen:** pass `/dev/bus/usb` and `/dev/snd`, and add the host's `audio`
group to the container.

**Why the group:** `/dev/snd/*` is `root:audio 0660`. On the host an ACL lets
the desktop user in - `getfacl` shows `user:tkaneko:rw-` - but an ACL does not
follow into a container, and the uid there is not the one it names. Without
`--group-add`, PortAudio does not fail: it **reports an empty device list**,
which looks like an unplugged array. That failure mode is why the Makefile
derives the id with `getent group audio` rather than hard-coding 29.

**What was checked:** the three transfer paths do not interfere. The camera goes
through libusb (RSUSB), the array's audio through ALSA, and its direction
readout through a USB control transfer. Measured on a 15 second session with all
of them running:

```
  video           451 frames over 15.02 s = 29.96 fps
  frame interval  33.4 ms median, 33.3 min, 33.5 max
  inertial        12574 samples (accel 400 Hz, gyro 399 Hz)
  length          15.040 s by header, 15.040 s by clock points (0.3 ms apart)
  residual        0.035 ms rms, 0.095 ms max
  direction       225 readings at 15.0 Hz
  overlap         15.00 s of both tracks
  every cross-check agreed
```

They sit on different USB controllers here (the camera on bus 002 at 5 Gbps, the
array on bus 001 at 12 Mbps), so this does not prove they would not contend on a
machine where they share one - a Raspberry Pi, for instance.

---

## 14. Measure the device offset from a handclap, and say how well

**Chosen:** `rrr/tools/calibrate.py`. Detect the impulse in the audio, find the
peak frame-to-frame difference in the video around it, take the difference.
Write nothing without `--apply`.

**Alternatives:** a flashing LED (needs hardware); asking the SDK (neither
device documents its internal latency); assuming zero (what showing `0` would
amount to).

**Why the accuracy is what it is:** the two halves are not comparable. An audio
onset is locatable to well under a millisecond - it is a step in energy. The
video half is "the frame where the hands met", which is only known to within one
frame interval. **So the frame rate bounds the result, at 33 ms.** A single clap
gives ±16.7 ms; N claps give ±16.7/√N, which is why the tool reports the spread
across claps and warns when they disagree by more than two intervals.

Verified on a synthetic session with 80 ms planted in it, which it measured back
as +80.0 ms.

**Cost:** somebody has to clap in front of the camera. Until they do,
`calibration.offset_s` stays null and every consumer is told the alignment is
unmeasured - which is the honest state, not a defect.

---

## 15. One package, `rrr`, rather than six top-level ones

**Chosen:** `rrr/{timeline,video,audio,recorder,server,tools}`, imported as
`from rrr.video import ArchiveSource`.

**Alternatives:** leaving `audio/`, `video/`, `timeline/`, `recorder/`,
`server/` and `tools/` at the top level, as they were; a `src/rrr/` layout with
a build backend, as the sibling `multimodal-spatial-awareness` repository uses.

**Why:** recordings made here are meant to be read by other repositories, and
those six names are ones any other project might also define. `import video` in
a process that had this checkout on its path was a coin toss, and a name
collision at that boundary looks like corrupted data rather than like a broken
import. `rrr` is what the rest of the repository already calls itself - the
`.rrdb` suffix, the `RRR_` environment prefix.

**Why not `src/`:** a `src/` layout earns its keep when the package is built and
installed, so that tests run against the installed copy rather than the working
tree. This project is not packaged - there is no `[build-system]`, and both the
Makefile and the container run it from the checkout with the repository root on
`PYTHONPATH`. Adding `src/` would mean adding a build backend and reordering the
image's `uv sync` around the source copy, for no benefit here. The sibling
repository packages itself and so keeps `src/msa/`; the layouts differ because
the answer to "is this installed?" differs.

**Cost:** every import statement, the Makefile, the Dockerfile's `CMD` and the
documentation changed at once. Mechanical, and cheapest before anything outside
this repository reads a session.

---

## 16. One sidecar written by a person, and what it may not be used for

**Chosen:** `events.jsonl`. A label plus both clocks, stamped when the mark
reaches the recorder, flushed on every write.

**Alternatives:** a column in the manifest (rewritten every second while
recording, so a mark would race the rewrite); naming conditions in the session
id (one session per condition, which means restarting the camera between runs
and losing auto-exposure settling each time); annotating afterwards against
playback (accurate, but it cannot record what was *done* - only what is visible
in what was recorded).

**Why it is needed:** a recording of an experiment is unusable without knowing
which stretch was which condition, and "speaker at 45 degrees, two metres" is
not recoverable from the audio. This is the only thing in a session that a
device did not measure.

**What it may not be used for:** aligning anything. A person presses a button
after noticing something, a few hundred milliseconds late and by a varying
amount, so a mark bounds a stretch rather than naming an instant. When the
instant matters it comes from the signal - an onset in the audio, which is
locatable to well under a millisecond - and the mark only says what that onset
was. Writing this down here because the file will look like a timestamp source
to anyone who finds it later.

**Cost:** it is the one part of a session nobody can check. A dropped frame is
counted and a filled audio gap is recorded, but a mark that was never pressed
leaves nothing behind. `inspect` does what little can be done - it fails if a
mark falls outside the recording, which catches a sidecar belonging to another
session - and the rest is procedure.

---

## 17. Export to a flat directory, rather than being imported

**Chosen:** `rrr.tools.export` writes a session out as plain files - PNG, CSV,
WAV - in a flat layout indexed by `manifest.json`. The analysis repository reads
that. It does not import this package.

**Alternatives:** letting the analysis side depend on this repository and use
`ArchiveSource` directly (no duplication, no second copy of a 200 GB recording,
but it couples two repositories that have opposite jobs and different release
rhythms - and `video.rrdb` is shaped for writing 54 MB/s, which is a constraint
the analysis side should never have to think about); ROS 2 bags, which every
SLAM tool reads but which drags rosbag into a repository that otherwise needs
numpy and torch; VRS, which the analysis side already reads for AEA but which
puts an SDK back in the middle.

**Why flat and role-named:** the alternative shape is EuRoC's `cam0`, `cam1`,
`imu0`. Removing a camera from a rig renumbers every later one, so an old
configuration file silently means something different. `ir_left` does not move
when `ir_right` goes away. The dataset this project compares against - AEA -
avoids the problem differently, by putting every stream inside one
self-describing VRS container and using the directory only to separate raw from
derived. Without a container, that self-description has to live in a manifest,
which is why `manifest.json` is the index rather than something a reader
reconstructs by walking directories.

**Four properties, each of which would hurt later if reversed:** names are roles
and not numbers; `manifest.json` answers what a session holds; every sampled
stream has an explicit time-bearing index plus `data/` when its samples are
files; anything variable-length is an array rather than a layout, so eight
microphones instead of four is a longer list and not a new directory.

Export format 2 names image files with a zero-padded sample id rather than
`t_ns`. `received_monotonic` can repeat when two sets are delivered back-to-back
(measured twice in 17,996 frames on Windows); using it as a filename could make
the second image silently replace the first. Each image index therefore keeps
`sample_id`, the archive's `group_id`, the common host `t_ns`, the stream's own
sensor timestamp and its timestamp domain. Variable per-frame firmware metadata
is retained separately as JSONL, keyed by the same `group_id`.

A frame-range export derives a half-open host-clock interval from the selected
frames and crops audio, inertial samples, DOA and marks to it as well. It is
written beside the destination, validated using only the neutral files, then
renamed into place. A failed conversion therefore leaves neither a plausible
partial result nor, with `--force`, the previous valid export destroyed.

**Everything is expressed against the depth stream's frame,** which is what the
SDK reports `depth_to_color` and the rest against, and on a D400 is the left
infrared imager. Transforms are a list where each entry names both ends, so a
rig with a different set of sensors produces the same shape of file with
different rows.

**What it costs:** a second copy on disk, and one conversion. Colour is written
as RGB because nothing outside the SDK reads packed YUYV; the packed original
stays in the archive, and the manifest says which encoding was used. Depth stays
raw z16 with its scale, and the two inertial streams are written separately
rather than resampled onto shared timestamps.

**What it refuses:** applying the measured device offset. It is written down and
left unapplied - baking one alignment into the files would make the decision
unrecoverable. Where something is unknown, `notes` says so rather than an
identity being substituted for it.

---

## 18. Clear the emitter toggle before every mode, because the firmware insists

**Chosen:** setting the depth projector's mode always writes
``emitter_on_off = 0`` first, then ``emitter_enabled``, and alternating writes
``emitter_on_off = 1`` last. Then both options are read back and a disagreement
is logged.

**Alternatives:** the two obvious orderings, both of which this firmware
refuses.

**What was measured** on a D455, firmware 5.17.3.10, over a fresh pipeline:

```
emitter_enabled = 0 ; emitter_on_off = 1   ->  REFUSED  hwmon 0x7b, Invalid parameter
emitter_enabled = 1 ; emitter_on_off = 1   ->  REFUSED  hwmon 0x7b, Invalid parameter
emitter_on_off = 0 ; emitter_enabled = 1 ; emitter_on_off = 1   ->  accepted
```

So it is not "the projector must be on first", which was the obvious guess.
The toggle has to be written as 0 and then re-armed, with the enable in
between. Asked for on its own immediately after ``pipeline.start()`` the toggle
is also accepted, which is why the bug only appeared when switching modes -
the first mode of a run worked and the third did not.

**Why it is worth a decision rather than a fix:** the refusal is silent in the
sense that matters. The option write fails, the log line scrolls past, and the
recording comes out with the projector solidly on while the session says
``alternating``. The infrared pair then carries the dot pattern that mode
existed to avoid, and nothing downstream can tell. Hence the read-back: what
the device reports is recorded, and a mismatch is warned about at the point it
can still be noticed.

**Cost:** alternating takes about four frames to settle, so the start of a
recording is not yet toggling. The per-frame laser power is in the metadata, so
this is visible rather than something to correct for.

**Verified:** on 150 constant, off 0 constant, alternating
``11110101010101...`` over 40 frames.

---

## 19. Scale the encoder thread count with the CPU, not a fixed number

**Chosen:** `DEFAULT_WORKERS = min(8, max(4, os.cpu_count() or 4))` in
`rrr/video/archive.py`, replacing a hard-coded `workers: int = 4`.

**Alternatives:** keep 4 everywhere; switch to `ProcessPoolExecutor` for true
parallelism instead of tuning the thread count.

**Why:** decision investigated in [windows-native.md](windows-native.md).
Four threads was this repository's default because it was the measured knee
on the reference machine (a 16-core i9-11900K): "18.3 ms per set... no
better with six." That knee is a property of that CPU, not of OpenCV's GIL
release. Measured on a 12-core/14-thread mobile chip (Windows, Core Ultra 7
265U), four threads held 42.2 ms/set against a 33.3 ms budget - real
video and IMU loss, not a rounding error - while eight held 26.2 ms/set.
`ProcessPoolExecutor` was tried as the alternative and was slower at every
worker count on that machine (51.2, 35.7, 33.5 ms at 4/8/12 workers): OpenCV
and zlib already release the GIL well enough that threads get real
parallelism, and Windows' process-spawn and array-transfer cost more than
the GIL was costing.

**Cost:** a weaker machine now spawns more threads doing nothing extra where
four was already enough (unmeasured whether this matters on a Raspberry
Pi's 4 cores - `min(8, max(4, 4))` still gives 4 there, so the existing
untested behaviour is unchanged). A stronger machine spawns marginally more
threads than its own knee might need, which the reference measurement says
costs nothing.

---

## 20. Fall back to the callback clock for the rest of a recording once the domain is known bad

**Chosen:** in `rrr/audio/capture.py`'s `_adc_time`, once the one-time check
finds `inputBufferAdcTime` on a different clock than `time.monotonic()`, set
a flag and use the `now - expected_lag` fallback for every later block in
that recording, not just the one being checked.

**Alternatives:** leave it as it was - warn once, then trust the reported
value regardless of what the check found.

**Why:** decision investigated in [windows-native.md](windows-native.md).
The check already existed and already logged "the two are probably not the
same clock" when it disagreed by more than a block's worth - but the
function still returned the disagreeing value. On a Windows PortAudio host
API this was measured returning something ~205,000-206,000 s away from
`time.monotonic()`. That value flows into `_fill_for`'s gap-length
arithmetic, which turned it into a gap sized in billions of samples, which
`wave.writeframes` refuses to accept - a session that logged a clear warning
about exactly this problem still crashed from trusting it anyway.

**Cost:** none beyond the coarser timing the fallback already documented for
the missing-value case - a few milliseconds, against a recording that
otherwise does not run at all.

---

## 21. Keep every frame; stop discarding a set for colour/depth skew

**Chosen:** `MAX_PAIR_SKEW_MS` and the discard it drove are removed.
`LiveSource` no longer rejects a set because its streams disagree about the
moment - every frame is kept, and colour and depth each carry their own
`get_timestamp()` (`FrameSet.color_timestamp_ms` / `depth_timestamp_ms`,
`frames.color_timestamp_ms` / `frames.depth_timestamp_ms` columns in the
archive) so a consumer can judge the disagreement for itself instead of
having it decided at capture time. `format_version` moves to 4: the old
`timestamp_ms` / `received_at` / `capture_monotonic` columns - one ambiguous
scalar, plus a value that quietly stopped being more accurate than arrival
time whenever the domain was not `global_time` - are replaced by
`color_timestamp_ms`, `depth_timestamp_ms` and `received_monotonic`, the one
field every set is guaranteed to have and the axis the audio recording is
also on. The reader still opens v1-v3 files, mapping their columns onto the
same three names.

**Alternatives:** widen the threshold for Windows; keep discarding but stop
counting it; build a custom colour-primary re-pairing scheme instead of
using the SDK's own bundled composite frame.

**Why:** investigated at length in
[windows-native.md](windows-native.md). `MAX_PAIR_SKEW_MS = 5.0` was
calibrated on Linux/RSUSB, where a correctly paired set is 0.03 ms apart and
5 ms exists only to catch a genuinely stale frame reused for hundreds of
milliseconds - two regimes two orders of magnitude apart in both directions.
On Windows (Media Foundation) the *normal* case measured 13 ms mean with
roughly 4 ms of jitter, later found to split into two separate causes: most
of the jitter came from auto-exposure's own frame-to-frame timing variance,
and a further slow, roughly-linear drift (tens of ppm to a few hundred) came
from colour and depth being timestamped independently, with no cross-sensor
correction (`global_time` is not achieved on Windows - domain reads
`system_time`). Neither number is a fixed offset a calibration could
subtract, and neither means a frame went missing: opening each sensor
directly and reading its own hardware-assigned `frame_number` (bypassing the
pipeline's syncer entirely) found zero gaps in colour, depth or either
infrared stream over a three-minute recording, in either auto-exposure
state. What the syncer's skew was catching, on this platform, was working
exactly as designed against a normal condition it was never calibrated for.

Given this repository's own priorities - no data loss first, then image
quality (auto-exposure stays on), with colour/depth timing reconciled by a
downstream consumer rather than guaranteed at capture time, since depth and
infrared are validation data for a SLAM pipeline that runs on colour - a
recorder that throws away real, undamaged frames because two independent
clocks disagree by low milliseconds is solving a problem downstream analysis
does not have and creating one it does (missing frames).

**Cost:** a reader wanting a synchronised pair now filters
`color_timestamp_ms` / `depth_timestamp_ms` itself rather than trusting every
stored set to already be one - the same work `_is_paired` used to do, now
done by whoever actually needs the guarantee instead of unconditionally at
capture time. `skipped_unpaired` is gone from every stats surface
(`VideoStats`, `VideoTrack`, the CLI's mispaired count); a session's own
health is instead visible directly from whether frames are missing, which
`skipped_duplicate` still reports.

---

## 22. Offer a raw codec for depth, colour and infrared

**Chosen:** `DEFAULT_CODECS` gains a `"raw"` option for all three streams -
`encode_depth_raw`/`decode_depth_raw`, `encode_plane_raw`/`decode_plane_raw`
(infrared and each YUYV plane), `encode_color_raw`/`decode_color_raw` (rgb8).
No compression at all: the array's own bytes, little-endian, straight into
the BLOB column. Selected per stream through `ArchiveWriter(codecs=...)` or
`RRR_DEPTH_CODEC` / `RRR_COLOR_CODEC` / `RRR_INFRARED_CODEC`.

**Alternatives:** a faster/weaker compression level (already at the fastest,
`PNG_LEVEL = 1`); a different lossless codec entirely; accept the drop rate
as a machine limit and do nothing.

**Why:** investigated in [windows-native.md](windows-native.md). Decision
19's own encoder-throughput figures - and this repository's, going back to
the original 18.3 ms/set measurement - were taken against synthetic noise,
not a real scene, and that turned out to matter: a compressor gives up
searching for redundancy in noise almost immediately, where real depth,
colour and infrared content has real redundancy to search for and takes
measurably longer to encode. Measured on real content on a Core Ultra 7
265U: ~29-30 ms/set for the full six-image lossless set, at any encoder
thread count from 8 to 12 - the CPU's own compute limit, not something more
threads fix. Raw removes the compute entirely.

**Cost, and where it does not help:** raw is roughly 3x the bytes of the
compressed set. For the *full* six-image set that cost is not academic: it
was measured moving the bottleneck rather than removing it. A synthetic
SQLite/WAL benchmark mirroring `archive.py`'s own write pattern found
compressed-size blobs insert at 10.2 ms each (`ArchiveWriter` was never
disk-bound) but raw-size blobs at 55.5 ms each - past the 33.3 ms budget on
its own, independent of any encoding cost. Recording colour, depth and
infrared as raw was measured losing frames at both ends: the SDK delivers
them at a clean ~30 fps regardless, but writing falls behind starting within
the first 10-20 seconds, in both the real `FrameHub`/`VideoWriter` path and
a minimal `LiveSource` + `ArchiveWriter` wiring with neither `FrameHub` nor
anything else of this repository's between them - ruling out `FrameHub` as
the cause for the full set specifically.

Where it *is* the fix: colour alone. Raw colour is about 61 MB/s, comfortably
inside what both encoding and SQLite can do, and a colour-only recording
using it was measured sustaining 29.99 fps with zero dropped frames over a
full 10-minute, 17,996-frame session - see windows-native.md for what closed
that specific gap (removing PNG's compute let a still-unexplained overhead
inside `FrameHub`/`VideoWriter` be absorbed rather than pushing the set over
budget).

---

## 23. Let which streams are captured, and their codec, be chosen from the CLI and the page

**Chosen:** `StreamConfig`'s three stream toggles (`color`, `depth`,
`infrared`) and `ArchiveWriter`'s per-stream codec (`"compressed"` or
`"raw"`, independently for each) are both now settable, not only through
`RRR_*` environment variables:

* the CLI gains `--no-color`, `--no-depth`, `--no-infrared` and
  `--color-codec` / `--depth-codec` / `--infrared-codec` (`rrr/tools/record.py`);
* the server's `PUT /api/settings` accepts `streams` (booleans) and `codecs`
  (`"compressed"`/`"raw"`) alongside the existing `sessions_dir`, refused
  while a recording is running for the same reason moving the directory is -
  a session cannot describe two configurations at once. A stream change
  restarts the shared `FrameHub`, the same way a resolution change would; a
  codec change needs nothing restarted, since the archive is created fresh
  at the start of each recording;
* the page gained a settings panel to drive both.

`SessionRecorder.streams` and `.codecs` are now settable properties
(mirroring the existing `.root` setter), refusing with `RecorderBusy` while
recording. `rrr.recorder.config.codec_for(stream, choice)` is the one place
"compressed" is translated into an actual codec name per stream (`zlib` for
depth, `png` for colour and infrared - `png16` stays reachable only through
`RRR_DEPTH_CODEC` for whoever wants it specifically).

**Alternatives:** a per-recording-start parameter instead of a persistent
setting (rejected: `sessions_dir` already established the "setting, changed
between recordings" shape, and inventing a second mechanism beside it for
streams and codecs would be two ways to configure a recording rather than
one); resolution/frame rate/emitter mode also made settable from here
(rejected as out of scope - those are the pipeline's own settled-at-start
parameters and stay environment-only, as they were).

**Why:** decision 21 and 22's investigation settled on a specific answer for
this Windows setup - colour alone, raw - but until now reaching it meant
setting three environment variables and restarting a process. What decisions
21 and 22 actually established is safe to make a first-class choice rather
than a workaround.

**The conclusion this exists to make usable:** on this Windows setup, **colour
alone with the raw codec** is the only combination measured to hold 30 fps
with nothing dropped. Depth and infrared can both be recorded here, and nothing
stops a session from asking for them, but every measurement so far shows a
real cost for doing so on this machine - see decision 22 and
[windows-native.md](windows-native.md). Operate accordingly until the
`FrameHub`/`VideoWriter` overhead below is root-caused or the machine changes.

---

## 24. Rank a matching-name capture device by host API and rate, not just by name

**Chosen:** `rrr/audio/capture.py`'s `_resolve_device` gathers every input
device whose name matches (as before), then picks among them in order: a
host API named `Windows WASAPI` whose `default_samplerate` agrees with the
requested rate; failing that, any device whose `default_samplerate` agrees;
failing that, the first name match, whatever it reports. It also no longer
gives up the moment the first name match cannot supply enough channels -
every match is considered before the "wrong firmware" error is raised.

**Alternatives:** hardcode a preference for the `"Windows WASAPI"` host API
by name alone, with no rate check (rejected: a future host API sharing that
name on different hardware would be preferred without ever being measured to
behave, which is the same mistake as trusting a name without evidence);
match on `default_samplerate` alone, with no host API check (rejected: a
different machine or driver update could plausibly report the array's real
rate through a host API that still behaves like MME in every other respect,
since only WASAPI was actually measured to behave); leave device selection
alone and instead widen or special-case something downstream (rejected: the
actual defect is at selection time - by the time a stream is open, which
host API's clock model applies is already decided).

**Why:** investigated in [windows-native.md](windows-native.md) §7. Windows
exposes this one physical array through four PortAudio host APIs at once,
and `_resolve_device` matched by name only, so it took whichever came first
in enumeration order - MME on this machine. Measured directly: MME's
`inputBufferAdcTime` is unfilled for half of all blocks and, for the rest,
about 5.3 days from `time.monotonic()`'s origin; WASAPI's is never unfilled,
offset by a *fixed* ~3.9 s (stable to 5.3 ms over five minutes), and reports
the array's real 16000 Hz rate where MME and DirectSound report the shared
mixer's 44100 Hz instead. Ranking by the pair (host API, rate agreement)
encodes exactly what was measured to matter, rather than either half of it
alone.

**Cost:** none beyond the extra `sd.query_hostapis()` call this needs. Linux
is unaffected - `hostapi` there is never `"Windows WASAPI"`, so tier 1 never
matches, and the array's single ALSA entry already agrees with the requested
rate, so tier 2 picks it exactly as tier 3 (the old behaviour) already did.

---

## 25. Initialise COM on the array's reader thread, for WASAPI's callback mode

**Chosen:** `AudioTap._run` - the background thread that reads the array -
calls `ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)` once,
before its first stream open, and `CoUninitialize` once, when the thread
exits. Windows only; `ctypes.windll` does not exist elsewhere, and no other
platform's PortAudio backend was measured to need this.

**Alternatives:** open the stream in blocking (read) mode instead of
callback mode (rejected: measured to avoid the failure, but changes the
capture model this class already has working reader-thread machinery for,
to work around a problem that has a smaller fix); depend on `pywin32` and
call `pythoncom.CoInitialize()` (rejected: adds a dependency for one API
call `ctypes` already reaches); initialise COM on every stream open inside
the reconnect loop, rather than once for the thread's life (rejected: it is
the thread, not any one stream on it, that needs an apartment - matches how
the domain calibration in decision 26 is also thread-lifetime state, not
per-connection state).

**Why:** investigated in [windows-native.md](windows-native.md) §7. Once
decision 24 picked WASAPI, the real `AudioTap` failed on its first stream
open with `PaErrorCode -9999` / `WdmSyncIoctl: DeviceIoControl GLE =
0x00000490`, reproducible in isolation as "background thread + callback
mode" specifically - blocking mode on the same thread, or callback mode on
the main thread, both work. `IAudioClient::Initialize` for an event-driven
(callback) stream is a COM activation and needs an apartment on the calling
thread; MME and DirectSound do not use COM this way, which is why this had
never come up before decision 24 changed which host API gets opened.

**Cost:** negligible - one `ctypes` call each way, once per thread lifetime.
A `CoInitializeEx` failure (`RPC_E_CHANGED_MODE`, if something else already
initialised COM on this thread in an incompatible mode) is logged and
otherwise ignored rather than raised, since only WASAPI's callback mode
actually depends on it.

---

## 26. Correct a stable clock-domain offset instead of discarding it

**Chosen:** `AudioTap._adc_time` collects `now - reported` over the first 20
valid blocks (`_DOMAIN_CALIBRATION_BLOCKS`, ~0.3 s) instead of deciding from
one, and uses the *spread* (standard deviation) of that window to tell three
cases apart: agrees with `time.monotonic()` (use `reported` as-is, decision
20's original good case); a fixed but different epoch, spread under
`_DOMAIN_STABILITY_S` (0.25 s) (add the measured mean offset to `reported`
for the rest of the recording, keeping the ADC clock's own precision); or
not stable at all (fall back to the callback clock for the rest of the
recording, decision 20's original bad case, unchanged). Every block during
the calibration window itself is timed from the callback clock, the same
conservative choice decision 20 already made for a missing reading.

**Alternatives:** keep decision 20's binary "agrees or discard" check
(rejected: measured, on WASAPI, to discard a clock that was actually good
and replace it with the callback's own arrival timing, which is not just
coarser but bursty under real thread scheduling - a live 15 s recording
produced ~150 spurious silence-fills and a +153,943 ppm fitted rate this
way, nearly the same shape of failure decision 20 already fixed once);
widen decision 20's acceptance window instead of adding a second tier
(rejected: would accept some genuinely incoherent domains too, since the
window would have to be wide enough to cover WASAPI's ~3.9 s offset, at
which point it no longer catches what it exists to catch); hardcode the
measured ~3.9 s offset as a constant (rejected: not measured to be stable
across machines, arrays, or driver versions - the whole point of measuring
here is to not assert what has not been measured on the array actually
plugged in).

**Why:** investigated in [windows-native.md](windows-native.md) §7. Decision
20's check could only say "the same clock" or "not usable" - it had no way
to express "a real, low-jitter clock, just on an epoch of its own", which is
what WASAPI's ADC time actually is (measured stable to 3.5-5.3 ms std across
three separate runs, against MME's 5-6 **seconds** of spread among the
blocks it filled in at all). A single-block check cannot even measure a
spread, which is why calibration needed to become a window rather than one
reading. Verified end to end through the real CLI: a 5-minute recording
fitted +1 ppm with 422 samples (26 ms) filled over 300 s, independently
confirmed by `rrr.tools.inspect`.

**Cost:** the first ~0.3 s of every recording is timed from the coarser
callback clock while calibration runs, which it also was, for one block
only, before this change. `rrr.tools.inspect`'s `RESIDUAL_WARN_MS` (1.0 ms,
calibrated against Linux/ALSA's 0.03 ms jitter) flags the resulting
first-point residual (measured 20.7 ms) on a Windows/WASAPI recording as a
`PROBLEM` - understood as this startup transient rather than adjusted away,
since the check is still correctly describing a real, if harmless, number.

---

## Known limits

**Nothing stops a recording when the disk fills.** At 195 GB an hour this will
happen. `ArchiveWriter` logs a write failure and continues, which would leave a
damaged session rather than a short one. Handled by watching the remaining-time
figure for now.

**Raspberry Pi is untested.** The image is built to be portable - RSUSB needs no
kernel module and the frontend is prebuilt - but neither the aarch64 build nor
the encoding throughput has been checked. Lossless at 54 MB/s will not fit: the
18.3 ms per set is a sixteen-core figure.

**Native Windows keeps every colour, depth and infrared frame, with no fixed
cross-sensor sync.** Media Foundation does not achieve `global_time`, so
colour and depth are timestamped independently rather than through one
drift-corrected clock (decision 21). A downstream consumer that needs a
synchronised pair filters `color_timestamp_ms` / `depth_timestamp_ms` itself.
WSL2 with librealsense built for `FORCE_RSUSB_BACKEND` was also measured: it
does restore `global_time` (skew under 1 ms), but `usbipd-win`'s USB/IP
tunnel could not sustain the full four-stream bandwidth in testing - a
different limitation, not a better answer, for the platform this repository
otherwise runs on. Both are written up in
[windows-native.md](windows-native.md).

**Native Windows cannot hold 30 fps recording depth or infrared, on this
machine.** Colour alone does, losslessly, using `raw` (decision 22) -
measured at 29.99 fps with zero dropped frames over ten minutes, and again at
29.69 fps over a 73-minute run through the actual server path with a live
preview attached concurrently (0.87% dropped, the only measurable cost of
going through the server rather than the CLI). Adding depth and/or infrared
measurably degrades it (colour+depth or colour+infrared: ~24 fps; all three,
compressed: ~18 fps) - raw does not fix this for more than colour alone,
since the full set is then disk-bound rather than CPU-bound (decision 22).

**Operate with colour alone and the raw codec on this machine** - decision 23
makes that a first-class choice from the CLI or the page rather than a set of
environment variables to remember. This is closed as the operating answer for
now, not because the cause is fully understood: a minimal `LiveSource` +
`ArchiveWriter` wiring with neither `FrameHub` nor `VideoWriter` involved
reaches a clean 30 fps for colour alone at the *compressed* codec, where the
real recording path through both classes reaches only ~25 fps at that same
codec - an overhead on the order of the gap between "compressed colour alone"
(~25 fps, measured through the real classes) and "colour alone, raw" (~30 fps,
measured the same way) that was never isolated to a specific line despite
targeted profiling (`dataclasses.replace`, the hub's locks, `_should_stop` /
`_superseded`, and running the read/write loop on a bare background thread
were each measured negligible or non-reproducing in isolation). Raw happens to
remove enough compute that the real path clears its budget regardless, which
is why this is closed rather than pursued further: the practical goal is met,
and the remaining question is a research one about where the overhead lives,
not a blocker to recording on this machine today. Worth reopening if a future
measurement needs compressed colour, or the full set, to also hold 30 fps
here.

**Native Windows records the array's audio cleanly once decisions 24-26 are
applied**, checked in three configurations, all through the real CLI or
server (no synthetic data):

| configuration | duration | fitted rate | filled |
|---|---|---|---|
| audio alone (`--no-video`) | 300.00 s | +1 ppm | 422 samples (26 ms) |
| audio alone, through the server, polled at 1 Hz like the page | 300.62 s | +4 ppm | 662 samples (41 ms) |
| audio with the camera recording at the same time (colour+raw) | 301.91 s | +16 ppm | 1,216 samples (76 ms) |

All three land the overwhelming majority of what they fill within the first
~0.3-0.4 s of the recording - the calibration window itself (decision 26),
which is timed from the coarser callback clock by design until it decides
whether the array's clock can be trusted as-is or needs a fixed correction.
Recording through the server or alongside the camera makes that startup
window somewhat noisier (up to ~72 ms worst departure, vs ~20 ms alone) but
does not introduce filled samples anywhere else in any of the three runs -
no evidence of USB bandwidth or CPU contention with the camera, or of the
server's own polling, costing anything once steady state is reached. The
camera's own numbers are unaffected either way: 8,996 frames at 29.99 fps,
0 dropped, in the combined run.

Separately, direction of arrival fails outright here regardless of any of
the above (`doa read failed: No backend available`) - a missing
libusb-compatible driver binding for the array's control interface, not
investigated as part of this work. See [windows-native.md](windows-native.md)
§7.
