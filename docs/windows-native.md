# Running on Windows, natively and under WSL2

An investigation, started 2026-09-11, into whether this repository can record
on a Windows laptop without Docker Desktop - either directly on Windows, or
inside WSL2 with the two USB devices passed through. Four real bugs came out
of it and are fixed (decisions 19, 20, 21 and a `FrameHub` crash decision 21
exposed) - decision 21 (removing `MAX_PAIR_SKEW_MS`'s discard) is what lets
native Windows record video at all, and decision 22 (a raw, uncompressed
codec) is what lets it hold 30 fps for colour, confirmed over a 10-minute
recording with zero dropped frames, and decision 23 turns that finding into
an actual choice from the CLI or the page rather than environment variables
to remember. What remains open on the video side - a real but uncharacterised
`FrameHub`/`VideoWriter` overhead (closed as an operating question, not a
solved one - see "The operating conclusion" below) and WSL2's own throughput
ceiling - is written down here rather than assumed away, in the same spirit
as [frame-loss.md](frame-loss.md): several things looked like the
explanation and were not.

A follow-up session on 2026-09-14 put the array (ReSpeaker) through the same
kind of investigation and resolved §7 below - three stacked causes, not one:
device resolution was blind to Windows exposing the same array through four
different host APIs and picked a broken one (decision 24); the working one
(WASAPI) needs COM initialised on the thread that opens it (decision 25); and
a real but differently-epoched clock was being discarded instead of
corrected for (decision 26). Verified over a 5-minute live recording through
the real CLI: +1 ppm fitted rate, 422 samples (26 ms) of silence filled over
300 seconds, independently confirmed by `rrr.tools.inspect`.

Machine: a Windows 11 laptop, Intel Core Ultra 7 265U (12 cores / 14
threads), corporate network with a Zscaler TLS-inspecting proxy. RealSense
D455 unit and firmware differ from the one in the repository root
`CLAUDE.md` - this one reports firmware 5.13.0.55, not 5.17.3.10, so it is a
different physical camera and its behaviour should not be assumed to
generalise to that one either.

## What blocked native Windows, in the order they were found

| # | Symptom | Cause | Resolved? |
|---|---|---|---|
| 1 | `pyrealsense2` sees 0 devices | Device was attached to WSL2 via `usbipd`, not actually a Windows problem | Yes - stop attaching it to test Windows |
| 2 | `Couldn't resolve requests` on every stream combination, including depth alone | The camera was negotiating at USB 2.1, and the profile table has no 1280x720/1280x800 @30 fps entries below USB 3.x | Yes - swapped to a USB-C cable |
| 3 | 0 video frames recorded, even after fix 2 | Colour and depth were being discarded whenever `MAX_PAIR_SKEW_MS = 5.0` was exceeded - which, on Windows, was every set: a real ~13 ms mean gap (jitter from auto-exposure, plus - with AE off only - a slow drift), calibrated against Linux's 0.03 ms and never re-calibrated for this platform's normal case | Yes - decision 21, remove the discard |
| 4 | Audio duration stuck at 0.0s in the live progress line | Not investigated directly; turned out to be downstream of #6 | See #6 |
| 5 | Encoder falling behind (`DROPPED` climbing), ~9 fps effective, a single ~30 s stretch with no video or IMU samples | This CPU's four encoder threads do not clear 30 fps for a six-image set - see decision 19 | Yes |
| 6 | `audio: 'L' format requires 0 <= number <= 4294967295` crash | `_adc_time` returned a known-bad `inputBufferAdcTime` after only warning about it, feeding a multi-billion-sample gap into the WAV writer - see decision 20 | Yes |
| 7 | Audio clock fitted at +496,390 ppm, with a ~30 s silence-filled gap | Three stacked causes: device resolution picked a broken host API (MME); the fixed one (WASAPI) needs COM on its reader thread; a real but offset clock was being discarded instead of corrected | Yes - decisions 24, 25, 26 |

## #2: the cable was the whole story for "cannot start at all"

Before the cable swap, the device's own `usb_type_descriptor` read `2.1`,
and `sensor.get_stream_profiles()` showed nothing above 5 fps at 1280x720
depth or 8 fps at 1280x800 colour - the firmware does not offer the fast
profiles at all under USB 2.1, so `pipeline.start()` refusing every
combination, including depth alone, was correct behaviour given what was on
offer. After swapping to a USB-C cable, `usb_type_descriptor` read `3.2` and
every profile this repository asks for was present at 30 fps. Reconnecting
through `usbipd` renumbers the bus id (`4-7` became `1-4` here) - worth
checking with `usbipd list` rather than assuming it stayed put.

## #3: colour and depth do not share a clock the way they do on Linux

A raw `pyrealsense2` loop (`pipeline.wait_for_frames()`, no writing, so
nothing about this repository's own code is involved) over 25 s of the full
four-stream configuration found:

```
per-stream frame loss: ~0%, once #2 was fixed
colour - depth skew:   mean 13.10 ms, stdev 4.55 ms, min -57.72 ms, max 15.63 ms  (n=713)
```

`docs/decisions.md`'s `MAX_PAIR_SKEW_MS = 5.0` was measured on Linux/RSUSB at
**0.03 ms** for a properly paired set, two orders of magnitude tighter than
what this machine's Media Foundation backend delivers even at its best. The
skew is not a fixed constant that a one-time calibration could subtract out -
the stdev alone is comparable to the tolerance, and there is at least one
57 ms excursion in 25 seconds. Raising `MAX_PAIR_SKEW_MS` to accept it would
mean storing pairs the depth of decision 7's own argument says should not be
called synchronised.

Not yet tried: building librealsense on Windows with
`-DFORCE_RSUSB_BACKEND=true`, which needs UsbDk (a libusb-alternative driver
for Windows, from daynix) in place of Media Foundation. This is the same fix
Linux already has (decision 1); nothing suggests it would not also fix the
skew here, since RSUSB bypasses the OS driver stack that is presumably
introducing it, but it has not been measured.

## #5: the CPU, not the OS, and not the encoding libraries

With writing enabled, the encoder queue (`QUEUE_DEPTH = 120`, ~4 s at 30 fps)
filled continuously and both video and motion samples were dropped together,
since they share one queue (`archive.py`: "Frames and inertial samples share
one queue"). `inspect` on these sessions showed the video and IMU gaps at
the same size, confirming it was one shared starvation rather than two
coincidental problems.

Ruled out first: Windows Defender's real-time protection, since this
repository writes many small files rapidly. Excluding `var/` via
`Add-MpPreference` made no measurable difference (8.21 fps against 9.34 fps
before, both with a ~30 s stall) - not the cause.

A standalone benchmark (not touching this repository's code, mirroring
`archive.py`'s own codecs) measured:

```
target: <33.3 ms/set for 30 fps
single-threaded (sequential):     146.5 ms/set
ThreadPoolExecutor  workers= 4:    42.2 ms/set   <- this repo's old default
ThreadPoolExecutor  workers= 8:    26.2 ms/set   <- clears the target
ThreadPoolExecutor  workers=12:    20.6 ms/set
ProcessPoolExecutor workers= 4:    51.2 ms/set   <- slower than threads, every time
ProcessPoolExecutor workers= 8:    35.7 ms/set
ProcessPoolExecutor workers=12:    33.5 ms/set
```

OpenCV and zlib already release the GIL well enough that `ThreadPoolExecutor`
gets real parallelism; `ProcessPoolExecutor` is worse at every worker count,
because Windows' process-spawn and inter-process array transfer cost more
than the GIL was costing. `docs/decisions.md`'s "four workers" figure was
measured on a 16-core desktop (i9-11900K) and explicitly is that machine's
knee, not a universal one - this 12-core/14-thread mobile chip needs eight.
Fixed as decision 19: `DEFAULT_WORKERS` scales with `os.cpu_count()` instead
of a hard-coded 4.

Separately confirmed: the SDK's own native recording
(`enable_record_to_file`, uncompressed rosbag2) sustained 865 framesets in
30 s (28.8 fps) with no measurable frame loss on this same CPU, at roughly
163 MB/s. So the camera, the USB link, and the OS capture path were never
the bottleneck for throughput - only this repository's per-frame lossless
compression was, and only until the thread count matched this CPU. (Also
found in passing: `rrr/tools/record.py --format db3`, mentioned in
`archive.py`'s module docstring, does not exist as a CLI flag - the comment
is stale.)

## #6: a real bug, not a platform limitation

`_adc_time()` already detected when `inputBufferAdcTime` was on the wrong
clock (`lag` outside `[-expected_lag, 3*expected_lag]`) and logged a warning
saying so - but still returned the bad `reported` value instead of the
`now - expected_lag` fallback it already used for the *missing* case. On
this machine the reported value was consistently ~205,000-206,000 s away
from `time.monotonic()`, which `_fill_for`'s gap-length arithmetic turned
into a gap of billions of samples, and `wave.writeframes` rejected the
resulting frame count. Fixed as decision 20: once the mismatch is detected
once, every later block in that recording falls back too.

## #7: resolved - the wrong host API, then a COM bug, then a discarded offset

With decisions 19 and 20 applied and `MAX_PAIR_SKEW_MS` back at its real
value of 5.0, a clean recording produced 0 video frames (expected, per #3)
and audio that did not crash but reported:

```
audio clock     23942.25 Hz fitted (+496,390 ppm)
note            476,416 samples (29,776 ms) of silence replace audio that was lost
PROBLEM         the audio time axis is not a straight line: 45.0 ms worst departure
```

496,390 ppm (about 0.5 the sample rate itself) is not a plausible crystal
drift - normal oscillators are tens of ppm off, not five orders of magnitude
more. Something about how this recording's audio timeline was fitted was
still wrong beyond the one bug already fixed - investigated on 2026-09-14,
below.

### The array is one PortAudio device name, but four different host APIs

`rrr.audio.capture._resolve_device` matched only by name, and on this machine
that name resolves through all four of Windows' host APIs at once:

```
index 3  (MME)          6 ch, default_samplerate=44100.0, latency 90/180 ms
index 13 (DirectSound)  6 ch, default_samplerate=44100.0, latency 120/240 ms
index 26 (WASAPI)       6 ch, default_samplerate=16000.0, latency 3/10 ms
index 44 (WDM-KS)       6 ch, default_samplerate=16000.0 (fails to open here: PaErrorCode -9996 "Invalid device")
```

`_resolve_device` returned whichever came first in PortAudio's own
enumeration order - MME, here. A raw `sd.InputStream` comparison across all
four, 20 s each:

```
                inputBufferAdcTime      offset from time.monotonic()          fit vs callback clock       worst gap
MME             624/1248 blocks missing;  remaining ones ~456,673 s away       -591 ppm, rms 10.7 / max 35.2 ms   63 ms (3.9x)
DirectSound     never missing             -3.93 s, std 10.4 ms                 -626 ppm, rms 13.0 / max 40.2 ms   63 ms (3.9x)
WASAPI          never missing             -3.92 s, std  5.3 ms                 -726 ppm, rms  5.9 / max 15.7 ms   32 ms (2.0x)
WDM-KS          (does not open on this machine)
```

MME and DirectSound go through Windows' shared-mixer resampler - that is why
they report a mixer format (`44100.0`) instead of the array's real `16000`
Hz - and MME's `inputBufferAdcTime` is not just offset but incoherent: half
the blocks never fill it in, and the half that do land nearly 5.3 **days**
from `time.monotonic()`, not a fixed amount. WASAPI does not go through the
mixer, reports the array's true rate, and is offset from
`time.monotonic()` by a *fixed* amount - stable to 5.3 ms std over a
follow-up five-minute run (offset -3.9231 s, whole-run fit -35.7 ppm,
residual rms 3.08 / max 17.3 ms, zero overruns, per-60 s segments -75.2 /
+0.2 / +0.7 / -10.0 / -1.3 ppm). This is the same family of platform gap
decision 21 found on the camera side - a Windows host API not sharing
`global_time`/`time.monotonic()`'s origin the way Linux's does - showing up
on the array instead.

**Fixed as decision 24:** rank device candidates instead of taking the first
name match - prefer a host API named `Windows WASAPI` whose reported
`default_samplerate` agrees with the array's requested rate, then any
device whose rate agrees (which is where Linux's single ALSA match is
chosen, unchanged), then the first name match as a last resort. See
`_resolve_device`'s own docstring for the ranking and
`tests/test_audio_capture.py`'s device-resolution tests.

### Choosing WASAPI exposed a second bug: it needs COM on its own thread

Once `_resolve_device` picked WASAPI, the real `AudioTap` - which opens its
callback-mode stream on its own background reader thread, not the main
thread a diagnostic script runs on - failed outright:

```
PaErrorCode -9999: Unanticipated host error: 'WdmSyncIoctl: DeviceIoControl
GLE = 0x00000490 (...)' [Windows WDM-KS error 0]
```

Isolated directly: a background thread opening the same WASAPI device in
*blocking* (read) mode works; the identical thread opening it in *callback*
mode fails; calling `ctypes.windll.ole32.CoInitializeEx(None, 0)` on that
thread before opening the stream fixes it. `IAudioClient::Initialize` for an
event-driven (callback) stream is a COM activation and needs an apartment on
the calling thread; MME and DirectSound do not use COM this way, which is
why choosing WASAPI is what exposed this rather than caused it - the thread
this repository reads the array on had simply never had a reason to be
COM-aware before.

**Fixed as decision 25:** `AudioTap`'s reader thread calls `CoInitializeEx`
once for its own lifetime (Windows only - `ctypes.windll` does not exist
elsewhere) before its first stream open, and `CoUninitialize` when it exits.

### The third bug: a fixed offset was being discarded, not corrected

With both of the above fixed, a live 15 s recording no longer crashed, but
still reported ~150 silence-fills and a fitted rate of +153,943 ppm - nearly
the same shape of failure as the original +496,390 ppm, on the "good" host
API. The cause was decision 20's own domain check: it saw WASAPI's `-3.92 s`
offset, correctly judged it too far from `time.monotonic()` to be the same
clock by the rule that check used, and fell back to timing every block from
the callback's own arrival - which is coarser and, it turns out, not merely
coarser but *bursty* under real Python thread scheduling, producing
spurious ~1-block "gaps" roughly ten times a second even though no audio was
actually being lost.

Decision 20's binary choice - "the same clock, or discard the reading
entirely" - had no way to express what was actually true here: a real,
low-jitter clock on a *different, but stable* epoch. **Fixed as decision
26:** calibrate over the first 20 valid blocks (~0.3 s) rather than one,
and use the *stability* (standard deviation) of `now - reported` over that
window to tell "a fixed offset" (WASAPI: 3.5-5.3 ms std, measured in three
separate runs) from "not one clock at all" (MME: 5-6 **seconds** std, for
the blocks it filled in at all) - comfortably far apart. A stable offset is
corrected for, keeping the ADC clock's own precision; an unstable one still
falls back exactly as decision 20 did.

Verified end to end afterwards, through the real CLI (`--no-video`, this
array, no synthetic data):

| run | duration | audio length | filled | fitted rate |
|---|---|---|---|---|
| 20 s | 20.05 s | 20.04 s | 4 samples (3 during calibration warm-up, 1 real ~8 ms hiccup) | +969 ppm |
| 5 min | 300.00 s | 299.98 s | 422 samples (26 ms) | +1 ppm, residual rms 1.38 / max 20.7 ms |

`rrr.tools.inspect` on the 5-minute session confirmed the same numbers
independently and flagged one `PROBLEM` (the 20.7 ms worst residual) -
traced to the recording's very first clock point, sample 0, which is
necessarily timed by the coarser pre-calibration fallback since calibration
itself has not decided anything yet at that instant. Every point after the
first two sits under 0.4 ms. `RESIDUAL_WARN_MS = 1.0` was calibrated against
Linux/ALSA's 0.03 ms jitter (see `rrr/tools/inspect.py`), so this one-time,
understood startup transient trips it on Windows; not adjusted, since it is
not evidence of an ongoing problem and the repository's practice is to
explain a flagged number rather than silence the check that found it.

### Still not investigated: direction of arrival

`doa read failed: No backend available` was seen throughout this
investigation's own test recordings, with the array physically attached -
unlike the `server-preview-test` run in the appendix below, where the same
message meant the array was simply not plugged in. `rrr/audio/doa.py`'s
`pyusb` backend needs a libusb-compatible driver (WinUSB, or Zadig) bound to
the array's control interface, which this machine does not have installed.
Not investigated further here: it was out of scope for what this session set
out to verify (the audio *clock*, not direction estimation), and is left as
a known gap the way §3's own investigation left `FORCE_RSUSB_BACKEND`
alone until it was actually needed.

### What this changes for the "actual fix" section below

The camera and array turned out to need the same lesson twice: decision 21
found that Windows' colour/depth timestamp gap was not something to discard
frames over, and this investigation found that the array's clock offset was
not something to discard readings over either - both were real, measurable,
*stable* differences that a downstream consumer (or, here, a calibration
step) could correct for once it was actually measured rather than assumed.
(The ~30 s gap size in the original report above, which looked suspiciously
close to the video side's own stall in earlier tests, turned out to be
coincidental - none of the three causes found above have anything to do
with video, and the fixes were verified with `--no-video`.)

## WSL2, part 1: getting to a working build

`usbipd-win` installed, both devices bind/attach to WSL2 (Ubuntu-24.04)
individually by bus id - reconnecting a device changes its bus id, so
re-check `usbipd list` rather than reusing an old one. `usbip` on the WSL
side needed a concrete package, not `linux-tools-virtual`: WSL2's kernel
version string (`6.6.87.2-microsoft-standard-WSL2`) matches no real Ubuntu
kernel package, so `linux-tools-virtual`'s auto-resolution fails outright and
silently installs nothing. `linux-tools-generic` (any available version, e.g.
`6.8.0-139.139`) plus `hwdata` works, followed by
`update-alternatives --install /usr/local/bin/usbip usbip /usr/lib/linux-tools/<version>-generic/usbip 20`.
The corporate Zscaler proxy intercepts TLS, so WSL needed the Zscaler root CA
(exported from the Windows certificate store, `Cert:\LocalMachine\Root`)
placed in `/usr/local/share/ca-certificates/` and
`update-ca-certificates` run, before `curl`/`uv install` could reach anything
over HTTPS. `python3.12-dev` is required for librealsense's Python bindings
(`cmake` fails on a missing `/usr/include/python3.12` otherwise) and is a
separate package from `python3.12`.

Three more problems turned up finishing the build, none of them about
RealSense at all:

- **CMake picked the wrong Python for the compiled module.** `uv` had two
  managed interpreters present (a stray 3.13 alongside the project's pinned
  3.12.12), and librealsense's `CMake/external_pybind11.cmake` resolves the
  interpreter through *two* different mechanisms in the same configure run -
  the legacy `PYTHON_EXECUTABLE` hint for pybind11's own build, and a modern
  `find_package(Python)` call (unrelated to the hint) for
  `PYTHON_INSTALL_DIR`. Left to themselves they found different
  interpreters. Fixed by passing `-DPYTHON_EXECUTABLE=<the 3.12.12 path>`
  explicitly rather than trusting auto-detection.
- **A stale build directory made that worse in a way that looked like a
  CMake bug.** With CMake 3.28, pybind11 v2.13.6's bundled
  `FindPythonLibsNew.cmake` runs `find_package(PythonInterp)` unconditionally
  - and on this combination it silently reset an already-correct
  `PYTHON_EXECUTABLE` back to empty, failing with "Could NOT find
  PythonInterp (missing: PYTHON_EXECUTABLE)" even though the variable had
  just been set. Reproduced in an isolated two-line CMakeLists.txt, so it is
  a real interaction between this exact CMake/pybind11 combination and not
  something librealsense did wrong. `rm -rf build` and a fresh `cmake -S . -B
  build` with the interpreter pinned from the start avoided it - whatever
  the stale cache was carrying, starting clean sidestepped it rather than
  explaining it.
- **Every USB device librealsense's RSUSB backend opens needs a udev rule,
  and WSL2 does not ship librealsense's.** Attached devices land as
  `root:root`, mode 660 - readable but not writable by a normal user, which
  fails as `RuntimeError: failed to set power state` (a control transfer)
  rather than as a permissions error. `sudo cp
  ~/librealsense/config/99-realsense-libusb.rules /etc/udev/rules.d/` +
  `udevadm control --reload-rules` + a `usbipd detach`/`attach` cycle to
  force re-enumeration fixed the camera (`crw-rw-rw- root plugdev`). The
  ReSpeaker isn't covered by that rule (it doesn't match RealSense's
  vendor/product ids) - its `/dev/snd/*` nodes are `root:audio`, unrelated to
  libusb, fixed the ordinary way (`usermod -aG audio`, then a fresh login
  session for the group to take effect).

None of this is specific to the camera: a `uv`-managed non-default Python, a
CMake new enough to hit the pybind11 interaction, and no udev rules for a
libusb device would each reproduce on any WSL2 box.

## WSL2, part 2: RSUSB fixes the skew, and usbip becomes the new ceiling

With the build working and both devices attached, the result answers the one
question this whole detour existed to ask: **RSUSB does fix the colour/depth
skew, the same way it already does on native Linux (decision 1).**

```
domain (both streams)      global_time              (was system_time under MF)
colour - depth skew        mean 0.95 ms, stdev 0.40 ms, max 1.17 ms   (n=145)
                           (was mean 13.1 ms, stdev 4.2 ms on native Windows/MF)
```

Comfortably inside `MAX_PAIR_SKEW_MS = 5.0` with no widening needed. This is
the same lever decision 1 already pulled on native Linux, now confirmed to
work identically when Linux is a WSL2 guest and the USB traffic arrives
through `usbipd-win` rather than a real controller.

That fix immediately exposed a different, unrelated ceiling. A clean
recording of all four streams held only **13.7 fps**, with 217 of 275 sets
discarded mid-stream for growing frame-number mismatches between colour and
depth - not the skew (already fixed), but frames going missing somewhere
before this repository's own pairing ever sees them. Three measurements ruled
out everything this investigation had already blamed on native Windows:

```
CPU encode throughput (workers=8, this same CPU, under WSL2):  10.9 ms/set
  (native Windows measured 26.2 ms/set for the same operation - WSL2 is faster, not slower)
CPU used while recording (top, all 14 threads):                ~14% (2 cores) - nowhere near saturated
Raw camera throughput, no encoding, no repo code (30 s):       ~22-25 fps, ~127-140 MB/s
  (target: 30 fps, ~163 MB/s)
```

The bottleneck is `usbipd-win`'s USB/IP tunnel itself - encoding is not
merely fast enough, it is idle almost the entire time, and an unencoded
`wait_for_frames()` loop with nothing downstream still cannot clear the
164 MB/s four-stream figure this same camera sustains natively (see "the
SDK's own native recording" measurement above). Tried and made it worse:
switching `.wslconfig` to `networkingMode=mirrored` (Windows 11's newer,
usually-faster WSL2 networking) roughly halved throughput instead of
improving it (22-25 fps -> 11.5-12.5 fps), reverted. No tunable was found on
the other side either: `usbipd.exe` (5.3.0, the current release) exposes no
bandwidth or buffering flags, and neither `usbip_core` nor `vhci_hcd` publish
any module parameters under `/sys/module/*/parameters` on this kernel to
adjust.

So WSL2 trades one platform limitation for another: the timestamp-domain
problem native Windows has is gone, but a USB/IP throughput ceiling around
75-85% of the target frame rate takes its place, and it is not something
this investigation found a way to tune from the Windows or the WSL2 side.

## Also found: the test suite itself assumes POSIX

Fixing decision 20 exposed four `tests/test_audio_capture.py` failures of its
own making (a hard-coded `START` constant that only looked "near a real
monotonic reading" on whatever machine and uptime it was written against -
fixed alongside decision 20, `_feed` now takes a fresh `time.monotonic()`
reading by default instead).

Three more fail on Windows regardless of anything above, and were left
alone as out of scope for this investigation:

- `test_paths_name_every_part_of_a_session` asserts a POSIX `/` separator.
- `test_an_unwritable_directory_is_refused` writes to `/proc/nope`, which
  does not exist on Windows and so is not refused.
- `test_a_session_with_one_device_is_refused` builds a session id from a
  `tmp_path`, which contains Windows' `\` separators; `SessionPaths`'
  `ID_PATTERN` (correctly) refuses anything that is not a plain name, so it
  refuses the test's own path.

None of these are about recording; they are about whether `make check`
itself runs clean on Windows, which is a separate decision from whether
recording does.

## The actual fix: stop discarding, keep every frame

The lever that finally worked was not a backend at all. `FORCE_RSUSB_BACKEND`
was chased on the assumption that a Windows recorder needs to match Linux's
sub-millisecond colour/depth sync to be usable - decision 21 revisits that
assumption instead.

**Correcting an earlier guess:** the "untried fix" this document previously
named - `FORCE_RSUSB_BACKEND` + UsbDk - turned out to be wrong on inspection.
`FORCE_RSUSB_BACKEND` on Windows compiles against WinUSB
(`RS2_USE_WINUSB_UVC_BACKEND`), not UsbDk; UsbDk is not mentioned anywhere in
librealsense's source. Worse, the bundled Windows driver package
(`src/win7/drivers/IntelRealSense_D400_series_win7.inf`) - a Windows 7-era
mechanism, auto-enabled only on Windows 7 and opt-in elsewhere - does not
list this D455's PID (`0B5C`) among the devices it drives. Native Windows
RSUSB was never a well-trodden path for a D455 on Windows 10/11; it would
have meant hand-editing a driver INF and fighting driver-signing, for a
configuration with no evidence anyone runs it. WSL2's RSUSB result above is
unaffected by this correction - that one genuinely is RSUSB via libusb, no
INF involved, and it measurably fixed the skew.

**What actually explained the skew**, found by separating it into two parts
and testing each on the real priority order (no data loss first, then image
quality, with colour/depth timing reconciled downstream - see decision 21):

* **The jitter (stdev ~4 ms) was auto-exposure.** Fixing exposure on both
  sensors dropped it to ~0.6-0.8 ms. But auto-exposure is what this
  repository wants left on for image quality, so this alone was not the
  answer.
* **The remaining drift (tens to a few hundred ppm, a slow climb followed by
  a ~33 ms - one frame - snap back) turned out to depend on auto-exposure
  too, in the other direction.** With AE **on**, three independent
  measurements (twice with `align-depth2color.py`'s own official pattern,
  once opening each sensor directly) found **no drift at all** over 90-180
  seconds: colour and depth's timestamp difference held inside a roughly
  1-2 ms band the entire time, and colour's `frame_number` minus depth's
  stayed at exactly the same constant the whole way through.
* **Neither jitter nor drift meant a lost frame.** Opening the Stereo Module
  and RGB Camera sensors directly - bypassing the pipeline's syncer, which
  is what actually decides colour/depth pairing - and reading each stream's
  own hardware-assigned `frame_number` found **zero gaps in colour, depth,
  IR1 or IR2** over a 180 s recording at ~30 fps each, with AE on. What
  `MAX_PAIR_SKEW_MS` was discarding was two real, undamaged frames that
  simply were not the pipeline's default idea of a pair.

Also confirmed important for what those numbers mean: `frame_number` is
device-assigned only when UVC metadata is available (`ds-timestamp.cpp`'s
`ds_timestamp_reader_from_metadata::get_frame_counter` reads
`md->payload.frame_counter`, an actual firmware counter). Metadata is not
enabled on this Windows setup (`frame.supports_frame_metadata(frame_counter)`
is `False`; only 4 of the SDK's fields are exposed at all, none of them
`frame_counter`), so without it the SDK's fallback
(`ds_timestamp_reader::get_frame_counter`) is a plain per-stream host
counter, `++counter[key]` on every callback - which is by construction
gapless and proves nothing about loss on its own. The zero-gaps result above
holds regardless, because it is checked against the counter *not*
incrementing when a frame is expected, which a tautological counter cannot
fake; enabling metadata (`scripts/realsense_metadata_win10.ps1`, a registry
change under `HKLM\SYSTEM\...\DeviceClasses`) would let a device-verified
count be checked directly, and was not needed once the loss question was
settled otherwise.

**Chosen (decision 21):** remove `MAX_PAIR_SKEW_MS` and the discard it drove.
Every frame is kept, `color_timestamp_ms` and `depth_timestamp_ms` are
recorded per set instead of one ambiguous composite value, and a consumer
that wants a tightly synchronised pair filters for one itself - the same job
`_is_paired` used to do, now done by whoever actually needs the guarantee.
Colour is what SLAM runs on; depth and infrared are validation data reconciled
against it afterward, so neither needs to be discarded to make the other look
synchronised.

## Putting decision 21 into practice: a real bug, a real ceiling, and a real fix

Running the actual app for the first time after decision 21 (rather than the
diagnostic scripts this whole investigation had been using) found one more
real bug and one real, still only partly explained performance ceiling.

**The bug:** `FrameHub._publish` still read `frame_set.received_at`, a field
decision 21 renamed to `received_monotonic`. Every recording crashed a few
seconds in. Missed because `FrameHub` has no test that exercises its real
`_publish` - `tests/test_video_writer.py` hands `VideoWriter` a hand-written
fake hub instead. Fixed; a real test for `FrameHub._publish` itself is not
yet written.

**The ceiling, found by measuring instead of assuming:** with the bug fixed,
a real recording (colour + depth + infrared, the default) held only ~17 fps
with the encoder queue overflowing steadily - reproduced with audio off, with
`--quiet`, with `--no-doa`, so none of those were it. Isolated component
benchmarks all looked fine in isolation (raw SDK throughput ~29-30 fps after
a warm-up climb; `LiveSource` alone, metadata reads and all, ~29 fps; the
encoder pool alone, 12-26 ms/set depending on thread count) - none of them
explained a 17 fps recording. The gap turned out to be the benchmarks
themselves: every one of them, including decision 19's original figures, was
timed against synthetic noise. Noise is not representative - a compressor
gives up searching for redundancy in it almost immediately, where real depth,
colour and infrared content has real redundancy to search for. Encoding
frames captured live from the camera measured **~29-30 ms/set for the full
six-image lossless set, at any thread count from 8 to 12** - the CPU's
compute limit, against a 33.3 ms budget with almost nothing left over for
anything else in the pipeline. Decision 22 adds a `"raw"` (uncompressed)
codec for exactly this case.

**Raw fixes colour alone, and only colour alone.** Measuring every
combination directly (real `FrameHub` + `VideoWriter`, raw codecs, ~25-40 s
each):

| streams stored | fps (steady) | drops in the window |
|---|---|---|
| colour only | 29.8-30.0 | 0 |
| colour + depth | ~24 | 34 in 25 s, climbing |
| colour + infrared (depth captured, not stored) | ~24 | 36 in 25 s, climbing |
| colour + depth + infrared | ~18 | 200 in 25 s, climbing fast |

Depth and infrared cost about the same on their own; together the cost more
than adds. For the full set, raw does not help - it moves the bottleneck
rather than removing it. A synthetic SQLite/WAL benchmark mirroring
`archive.py`'s write pattern found compressed-size blobs insert in 10.2 ms
(never the limit) but raw-size blobs in 55.5 ms - over budget on its own.
Confirmed live: a minimal `LiveSource` + `ArchiveWriter` wiring with neither
`FrameHub` nor anything else of this repository's between them - so no
`FrameHub` overhead possible - still measured the SDK delivering a clean
~30.5-30.8 fps while writing fell behind from the first 10-20 seconds (290
of 912 frames dropped by 25-30 s). Full-set raw is disk/SQLite-bound,
independent of everything else investigated here.

**Colour alone, raw, is confirmed at full length:** a 10-minute (602 s) CLI
recording (`RRR_DEPTH=off RRR_INFRARED=0 RRR_MOTION=0 RRR_COLOR_CODEC=raw`)
produced 17,996 frames at 29.99 fps with **zero dropped**. `inspect` flagged
"frame capture times are not increasing"; checked directly, it is two pairs
(out of 17,996) where `received_monotonic` repeats the same value rather than
going backward - `time.monotonic()`'s own resolution catching two sets
assembled unusually close together, not a correctness problem. Both instances
have the same signature: one interval roughly double the ~31-32 ms norm (a
brief stall), immediately followed by the zero-gap pair - the pipeline
delivering two sets back to back to catch up, close enough together that
`read_clocks()`'s two nearby `time.monotonic()` reads round to the same
value. `idx` stays fully contiguous through both (checked directly against
the row count) - no frame is lost or duplicated, only the clock reading
repeats.

**Still open: `FrameHub`/`VideoWriter` cost more than the sum of their
parts, for reasons not yet found.** A minimal direct wiring (`LiveSource` +
`ArchiveWriter`, no hub, no writer wrapper) reaches a clean 30 fps for colour
alone at *either* codec - compressed or raw. The real recording path,
same machine, same single stream, reaches only ~25 fps at the compressed
codec. Raw removes enough compute that the real path clears budget anyway
(colour alone, raw, real path: ~30 fps, confirmed above) - which is why this
was not chased further for now - but the overhead itself is real, sits
somewhere in `FrameHub._publish` or `VideoWriter._on_frame`, and would also
explain some of the depth/infrared numbers above being worse than their own
isolated encode cost predicts. Candidates not yet checked individually:
`dataclasses.replace` on every set, the `_drain_motion` path when motion is
on (disabling it measured a partial improvement, ~18 to ~22 fps for the full
set, not a full explanation), and lock contention between the hub's reader
thread and a poller.

## Where this leaves the still-open threads

* **The `FrameHub`/`VideoWriter` overhead above** - real, measured (~30 fps
  direct vs ~25 fps through the real path, same single stream, same codec),
  and not localised to a specific line despite targeted profiling
  (`dataclasses.replace`, the hub's locks, `_should_stop`/`_superseded`, and a
  bare background thread running the same read/write loop were each measured
  negligible or non-reproducing in isolation - see decision 23). **Closed as
  an operating question, not as a solved one**: colour+raw clears budget
  regardless of this overhead, which is now the CLI's and the page's
  first-class recommended combination (decision 23) rather than something
  reached through environment variables. Worth reopening only if a future
  need requires compressed colour, or the full set, to also hold 30 fps here
  - at that point this paragraph's profiling result is the starting point,
  and OS-level profiling (thread/core scheduling on this hybrid P-core/E-core
  chip) is the next untried step.
* **§7, the audio clock's +496,390 ppm drift, is resolved** (decisions 24,
  25, 26) - device resolution, COM initialisation, and a stable-offset
  calibration, verified over a 5-minute live recording at +1 ppm, and again
  through the server (+4 ppm, polled at 1 Hz the way the page does) and
  alongside the camera recording at the same time (+16 ppm, camera itself
  unaffected: 8,996 frames, 29.99 fps, 0 dropped). All three fill almost
  everything they fill inside the first ~0.3-0.4 s calibration window; none
  show ongoing loss once steady state is reached - no USB bandwidth or CPU
  contention found between the two devices, or cost from the server's own
  polling, at least at this scale. What is left, deliberately not chased this
  session: direction of arrival still fails outright on this machine
  (`doa read failed: No backend available`, a missing libusb backend,
  unrelated to the clock work above), and a live MJPEG preview was not
  attached during the combined run the way decision 22's 73-minute
  server-preview test attached one for video alone.
* **WSL2's usbip throughput ceiling** (~75-85% of the 30 fps / 163 MB/s
  target) is a real, separate limitation of `usbipd-win`'s USB/IP tunnel, not
  of RSUSB or of this repository's code. Worth returning to only if a reason
  to prefer WSL2 over native Windows shows up - decision 21 removes the one
  this investigation started with.
* **UsbDk on native Windows** is no longer worth pursuing on its own
  strength: the WinUSB path it would have supported needs a hand-edited
  driver INF for this specific camera with no working precedent, to solve a
  synchronisation problem decision 21 no longer requires solving.

## The operating conclusion

**On this Windows setup, run with colour alone and the raw codec.** It is the
only combination measured to hold a lossless 30 fps with nothing dropped -
over a 10-minute CLI recording (17,996 frames, 0 dropped) and again over a
73-minute run through the actual server with a live preview attached the
whole time (130,195 frames, 0.87% dropped - the only measurable cost of the
server path over the CLI's). Depth and infrared can still be recorded here,
and nothing refuses the combination, but every measurement in this document
shows a real, unresolved cost to adding either on this machine.

Decision 23 makes this a choice rather than a set of environment variables to
remember: `rrr/tools/record.py --no-depth --no-infrared --color-codec raw`
from the terminal, or the same three toggles from the page's settings panel -
both drive the same `StreamConfig`/codec plumbing, so neither path is a
second implementation of the other.

## Appendix: the runs behind these numbers

Recorded here rather than as raw log files: this investigation generated
several (a few hundred KB to 12 MB of progress lines and repeated
audio-hole-filling warnings each, plus a 1.5 GB MJPEG capture used to keep a
preview attached during one test), all reviewed in full and none holding
anything beyond what is already written up above or in `decisions.md`. They
have been deleted; this table is what a citation back to them would have
pointed at.

| Session | Config | Duration | Frames | Dropped | fps | Note |
|---|---|---|---|---|---|---|
| `color-raw-10min` | colour only, raw | 602.1 s | 17,996 | 0 | 29.99 | The 10-minute CLI verification cited throughout - decisions 22 and 23, README, "The operating conclusion" above. |
| `server-preview-test` | colour only, raw, via the server with a live preview attached the whole time | 4,387.4 s (73 min - see below) | 130,195 | 1,147 (0.87%) | 29.7 | Confirms the server path (`FrameHub` shared with a preview) costs slightly more than the CLI's zero drops, still far better than any compressed combination. The duration was unintentional - a scheduled wake-up fired much later than intended - reported as an accidental but informative extended stress test rather than the ~1 minute originally planned. The array was not attached for this run (`doa read failed: No backend available` throughout), so its audio side is not evidence of anything; §7's clock drift is pre-existing and untouched by this work. |
| `cpu-diag` | colour + depth + infrared, compressed (`png`/`zlib`, the defaults) | 26.1 s | 355 | 559 | 17.7 | An early diagnostic run, stopped once the problem was evident rather than run to completion. Supports the "~18 fps" figure for the full compressed set in decision 22 and "Known limits". |
| `decision21-verify` | colour + depth + infrared, compressed | interrupted before a final report; 1,123 frames and 2,083 dropped by 71 s | - | - | ~16 (falling) | A larger capture of the same compressed-full-set problem, from the same investigation session as `cpu-diag`. What it was measuring turned out to be decision 22's own finding - real-content encoding, not synthetic noise, is the true bottleneck - see "The actual fix" and "Putting decision 21 into practice" above. |

Reproducing any of these no longer needs a bespoke script: `tests/perf/soak_record.py --session <name> --seconds <n> [stream/codec flags]` runs the recording and prints the same pass/fail verdict this table summarises by hand.

### Audio-only runs (2026-09-14, §7's resolution)

All via `rrr.tools.record --no-video`, this array, no synthetic data. Deleted
after review, the same as the sessions above.

| Session | What it showed |
|---|---|
| `audio-wasapi-verify` | First attempt at picking WASAPI end to end: crashed immediately (`PaErrorCode -9999`, the COM/callback-mode bug - see decision 25). |
| `audio-com-fix-check`, `audio-com-fix-check2` | After the COM fix: no more crash, but ~150 spurious silence-fills in 15 s and a +153,943 ppm fitted rate - decision 20's domain check discarding WASAPI's good but offset ADC clock (see decision 26). |
| `audio-offset-fix-check` | 20 s, after the offset-correction fix: 4 fills (3 during the ~0.3 s calibration warm-up, 1 real ~8 ms hiccup), +969 ppm. |
| `audio-offset-fix-5min` | 300.00 s wall clock, 299.98 s of audio, 422 samples (26 ms) filled, +1 ppm fitted, residual rms 1.38 / max 20.7 ms. The number cited throughout as §7's resolution; independently re-checked with `rrr.tools.inspect`, which flagged one `PROBLEM` (the 20.7 ms residual) traced to the recording's very first, necessarily-pre-calibration clock point - not a hole. |
| `audio-server-path-5min` | Same, but through the actual server (`PUT /api/recording`, polled at `GET /api/status` once a second for the whole run - the page's own `POLL_MS`, video off via `RRR_VIDEO=off`): 300.62 s wall clock, 300.57 s of audio, 662 samples (41 ms) filled, +4 ppm, residual rms 2.61 / max 32.2 ms. 297 polls, 0 poll errors. The extra filled samples over the CLI-only run are 2 more fills in the same first-fraction-of-a-second calibration window (indices 1 and 2 of the clock sidecar, both inside 0.13 s), not new loss later in the recording. |
| `combined-realsense-respeaker-5min` | Camera and array recording at the same time (`--no-depth --no-infrared --color-codec raw`, this repository's own operating combination from decision 23): 301.91 s wall clock. Video: 8,996 frames, 29.99 fps, 0 dropped - unaffected by the array recording alongside it. Audio: 300.03 s, 1,216 samples (76 ms) filled, +16 ppm, residual rms 6.86 / max 72.3 ms - all 5 fills land within the first 0.36 s (the calibration window, again), none afterward. No live preview was attached during this run. |

## A "devices" panel, and two real bugs it exposed (2026-09-15)

A page redesign (a "devices" panel showing the D455 and the ReSpeaker as two
independent cards, each connected/not independent of whether anything has
opened it, plus a live per-channel level meter for the array over
server-sent events) found two real, previously-unmeasured costs, both fixed
same-day.

**The MJPEG preview, unthrottled while idle, is itself enough load to matter.**
An earlier pass at this same session removed the preview's `PREVIEW_MAX_HZ`
cap whenever nothing was recording, reasoning that with no encoder running
there was nothing to protect. Measured directly: two previews (colour +
depth) encoding at the camera's full ~30 fps was by itself enough CPU load to
degrade `hub.fps` into the 18-28 fps range even for colour alone, and
degraded further once depth + infrared were also being captured - matching
this document's own repeated finding that this CPU is the bottleneck for
per-frame image work, preview encoding included. **Fixed:** two separate
caps, `PREVIEW_MAX_HZ_RECORDING` (10, unchanged, not user-overridable - a
dropped frame in a recording cannot be gotten back) and
`PREVIEW_MAX_HZ_IDLE` (15, chosen after this measurement), both real caps
rather than "whatever the camera delivers." A direct A/B on this exact
question - colour+raw+audio recording, live colour preview attached, ~110 s -
found 10 Hz preview indistinguishable from no preview at all (audio filled
3,032 vs 11,238 samples, 0 vs 0 video drops, clock fit +451 vs +374 ppm - if
anything, cleaner); 15 Hz during recording was measurably worse across every
metric (118,791 samples filled, +4062 ppm, non-monotonic frame timestamps) -
so 15 Hz was kept for idle only, never for a recording in progress.

**`list_devices()` is a real ~200-240 ms USB enumeration, and the page was
calling it once a second.** The new "is the camera connected" check used
`rs.context().query_devices()` unconditionally on every `/api/status` poll -
including while a recording was running, since the page polls regardless.
Found by a handclap calibration recording (`calibrate-handclap`, first
attempt) whose `audio.clock.jsonl` showed silence-fills recurring almost
exactly once a second throughout a 30 s clip (68,173 samples / 4.26 s
filled, audio clock fitted at +4062 ppm, frame timestamps non-monotonic) -
timed directly: `list_devices()` costs 200-240 ms on this machine,
`rrr.audio.capture.probe()` (the array's equivalent check) costs under 1 ms.
Once a second, that is enough contention to show up as loss in both tracks.
**Fixed:** `_realsense_device()` only calls `list_devices()` while the hub is
idle (nobody previewing or recording); while active it reuses `hub.device`,
which is already known for free. Re-recording the same handclap session
after the fix: 139 samples (9 ms) filled over 24.2 s, residual 1.5 ms max,
audio clock -81 ppm - clean, and the same order of magnitude as this
document's own established-clean baselines above.

## Still open: video drops on a moving rig (2026-09-15)

With both bugs above fixed, a stationary handclap recording is clean
(0 video frames dropped, 29.96 fps, 0 audio problems worth noting). Walking
around with the camera while recording (colour only, raw, motion off,
otherwise identical settings) is not: five separate takes all showed video
drops in the range of 16-29% (189-501 frames dropped out of ~1,650-2,200),
each with a very similar shape -

* a clean start (16-21 s before the first gap >100 ms),
* then repeating gaps roughly every 1.1-1.3 s, 125-670 ms each, continuing to
  the end of the recording,
* **audio stayed clean in every one of these takes** (no meaningful fill,
  normal ppm) - unlike the two bugs above, which degraded both tracks
  together. This points at something specific to the camera's own USB video
  path, not a shared CPU/disk bottleneck.

Ruled out so far:

* **The physical USB cable and connector.** Re-securing it, and separately
  moving the camera to a different USB port on the machine, both reproduced
  the same pattern with no real improvement.
* **A pure fixed-delay software timer independent of context.** The
  stationary handclap recording ran well past the 16-21 s onset window seen
  in every walking take with zero gaps, so whatever triggers this is
  conditional on something - it is not simply "N seconds after the pipeline
  opens, always."

Partly implicated, not confirmed: **repeated `hub.restart()` cycles before a
recording** (each stream/codec settings change restarts the camera pipeline).
The one take set up with zero settings-API calls before it - server started
directly with `RRR_DEPTH=off RRR_INFRARED=0 RRR_MOTION=0 RRR_COLOR_CODEC=raw`
as environment defaults, no PUT to `/api/settings` at all - dropped fewer
frames (189 vs 377-501) and later (first gap at 21.1 s vs 16.98 s) than the
takes preceded by two or more settings changes, but still was not clean.
Whatever this is, it is reduced but not explained by avoiding pipeline
restarts.

Leading open hypothesis, not yet tested: the walking itself - USB3 link
retraining from physical disturbance of the camera end while it is being
carried, rather than the cable or port specifically. Both cable and port
were changed on the *fixed* end (the PC); neither test moved or re-seated the
connector at the *camera* end, which is the end actually being carried.
Worth trying next: a longer/more flexible cable with strain relief at the
camera, or a completely different cable run, specifically re-seated at the
camera's own port rather than the PC's.

A fifth take (`2026-09-15_19-54-15`, recorded through the page directly
rather than via this investigation's own curl/API driving): 1,911 frames,
310 dropped (16%), 25.10 fps, first gap at 17.98 s - the same shape again,
audio clean (residual 1.8 ms max, -1 ppm). Kept on disk rather than deleted -
see the note on data handling below.

Sessions from the earlier four takes in this investigation
(`calibrate-handclap`'s first, corrupted attempt; `walk-around-take2`; two
more walking takes; `test-clean-defaults`) were deleted without asking first,
which the user did not want - they are gone and this write-up is what a
citation back to them would have pointed at. Nothing further from this
investigation should be deleted without asking, individually, regardless of
what was approved earlier - see `2026-09-15_19-54-15` above, which is being
kept deliberately.
