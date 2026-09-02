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
`video.types.join_yuyv` does, and `server.preview.to_bgr_from_planes` skips the
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
before the first frame and have gaps while the sensor settles; `tools/inspect`
looks for gaps only inside the video's own span for that reason.

`FrameSet.motion` still exists, holding the newest buffered sample of each
stream, because a preview or a quick attitude estimate wants one number per
frame. It is not written to the archive - storing a fourteenth of the data
twice - and its docstring says what it is.

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
