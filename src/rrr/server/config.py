"""Runtime configuration for the server.

Every value can be overridden through the environment, so the same image runs on
this machine and on a Raspberry Pi without editing anything. What to record
lives in :mod:`recorder.config`; this is only about serving it.
"""

from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    """Read a boolean from the environment."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


#: Where to listen. Every interface, so the page can be opened from another
#: machine - a recorder on a Pi is usually driven from a laptop.
HOST = os.environ.get("RRR_SERVER_HOST", "0.0.0.0")

#: Port for the control plane.
#:
#: 8040 because 8000, 8020 and 8030 are in use on this machine (the latter two
#: are the playgrounds this borrows from) and 8010 belongs to another project.
#: Pinned rather than auto-selected: a server that silently lands somewhere else
#: is worse than one that refuses to start.
PORT = int(os.environ.get("RRR_SERVER_PORT", "8040"))

#: Seconds to wait for open connections before shutting down anyway.
#:
#: Must be finite. An MJPEG response ends only when the client disconnects, so
#: an unbounded graceful shutdown never completes - and a server that will not
#: stop is a camera that cannot be reopened.
SHUTDOWN_TIMEOUT_S = int(os.environ.get("RRR_SHUTDOWN_TIMEOUT_S", "5"))

#: How long the camera stays open after the last viewer leaves.
#:
#: Longer than the hub's own default: reloading a page drops every connection
#: for a moment, and reopening a RealSense pipeline costs about a second plus
#: however long auto-exposure takes to settle. A recording holds the camera by
#: its own reference, so this cannot end one.
IDLE_SHUTDOWN_S = float(os.environ.get("RRR_IDLE_SHUTDOWN_S", "20"))

#: Seconds to wait before reopening the camera after a failure.
RECONNECT_DELAY_S = float(os.environ.get("RRR_RECONNECT_DELAY_S", "2"))

# -- the preview -------------------------------------------------------------

#: Width the preview is scaled to before encoding.
#:
#: Measured at 1280x800: rendering, scaling to 640 and encoding costs 5 ms for
#: colour and 9 ms for depth. Sending full size instead would triple the bytes
#: to fill a panel that is 640 wide on screen anyway.
PREVIEW_WIDTH = int(os.environ.get("RRR_PREVIEW_WIDTH", "640"))

#: JPEG quality for the preview.
JPEG_QUALITY = int(os.environ.get("RRR_JPEG_QUALITY", "80"))

#: Upper bound on preview frame rate while a recording is running.
#:
#: Measured directly rather than assumed (2026-09-15): a colour+raw+audio
#: recording with a live colour preview attached at 15 Hz filled 118,791
#: audio samples (7.4 s) over 118.7 s and fitted the audio clock at +4062 ppm,
#: against 11,238 samples (0.7 s) and +374 ppm with no preview attached at
#: all over a 176.9 s recording of the same configuration - roughly 16x more
#: loss with the preview open. The 15 Hz test also showed non-monotonic frame
#: timestamps and a 320 ms IMU gap that the no-preview run did not. Back to
#: 10, which is what the 73-minute server-preview-test session in
#: docs/windows-native.md measured clean for video - but that test had no
#: audio attached, so 10 Hz is carried forward as the safer prior rather than
#: itself confirmed clean for this three-way combination.
PREVIEW_MAX_HZ_RECORDING = float(os.environ.get("RRR_PREVIEW_MAX_HZ_RECORDING", "10"))

#: Upper bound on preview frame rate while nothing is recording.
#:
#: Higher than the recording limit, but still a real cap rather than "however
#: fast the camera delivers" - measured on this machine (see
#: docs/windows-native.md): two MJPEG previews encoding at the camera's full
#: ~30 fps is by itself enough CPU load to stall the frame hub once depth and
#: infrared are also being captured, which reads as the preview freezing
#: rather than as a smooth 30 fps. 15 Hz is comfortably below where that
#: happened and still reads as close to real time.
PREVIEW_MAX_HZ_IDLE = float(os.environ.get("RRR_PREVIEW_MAX_HZ_IDLE", "15"))

#: Depth colour scale defaults. The page overrides these per request.
DEPTH_NEAR_M = float(os.environ.get("RRR_DEPTH_NEAR_M", "0.3"))
DEPTH_FAR_M = float(os.environ.get("RRR_DEPTH_FAR_M", "6.0"))
DEPTH_COLORMAP = os.environ.get("RRR_DEPTH_COLORMAP", "turbo")

# -- the audio level meter -----------------------------------------------------

#: Updates a second for the devices panel's live per-channel level meter.
#: Matches the video preview's own rate: fast enough to read as live, far
#: below what would compete with the encoders or the audio writer for CPU.
AUDIO_LEVEL_HZ = float(os.environ.get("RRR_AUDIO_LEVEL_HZ", "10"))

#: Seconds of audio each level is measured over.
AUDIO_LEVEL_WINDOW_S = float(os.environ.get("RRR_AUDIO_LEVEL_WINDOW_S", "0.1"))

# -- the frontend ------------------------------------------------------------

#: Built frontend to serve, if it has been built. Vite serves it on its own port
#: during development, so this being absent is normal rather than an error.
STATIC_DIR = os.environ.get("RRR_STATIC_DIR", "web/dist")

#: Vite runs on another origin during development, and the page may be opened
#: from another machine on the LAN.
ALLOW_ORIGINS = os.environ.get("RRR_ALLOW_ORIGINS", "*").split(",")

#: Whether changing the recording directory over HTTP is allowed.
#:
#: On by default because it is the one setting that has to be changeable while
#: the app is running - recordings are 195 GB an hour, so which disk they land
#: on is a decision made per session, not per deployment. It is a local tool, so
#: the path is not validated against a whitelist; it is checked for being a
#: writable directory and nothing more.
ALLOW_SETTINGS_WRITE = _flag("RRR_ALLOW_SETTINGS_WRITE", True)
