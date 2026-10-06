"""Runtime configuration for the API, each value overridable from the environment.

What to record lives in :mod:`rrr.recorder.config`; this is only about serving it.
"""

from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    """Read a boolean from the environment."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


#: Every interface, so the page can be opened from another machine.
HOST = os.environ.get("RRR_API_HOST", "0.0.0.0")

#: Pinned: refusing to start beats silently landing elsewhere.
PORT = int(os.environ.get("RRR_API_PORT", "8040"))

#: Seconds to wait for open connections before shutting down anyway. Finite,
#: because an MJPEG response ends only when its client leaves.
SHUTDOWN_TIMEOUT_S = int(os.environ.get("RRR_SHUTDOWN_TIMEOUT_S", "5"))

# -- the preview -------------------------------------------------------------

#: Width the preview is scaled to before encoding: the panel's own width.
PREVIEW_WIDTH = int(os.environ.get("RRR_PREVIEW_WIDTH", "640"))

#: JPEG quality for the preview.
JPEG_QUALITY = int(os.environ.get("RRR_JPEG_QUALITY", "80"))

#: Preview rate caps, recording and idle. Measured in docs/windows-native.md,
#: "A devices panel, and two real bugs it exposed".
PREVIEW_MAX_HZ_RECORDING = float(os.environ.get("RRR_PREVIEW_MAX_HZ_RECORDING", "10"))
PREVIEW_MAX_HZ_IDLE = float(os.environ.get("RRR_PREVIEW_MAX_HZ_IDLE", "15"))

# -- the audio level meter -----------------------------------------------------

#: Updates a second for the devices panel's level meter, the preview's own rate.
AUDIO_LEVEL_HZ = float(os.environ.get("RRR_AUDIO_LEVEL_HZ", "10"))

#: Seconds of audio each level is measured over.
AUDIO_LEVEL_WINDOW_S = float(os.environ.get("RRR_AUDIO_LEVEL_WINDOW_S", "0.1"))

# -- the frontend ------------------------------------------------------------

#: The page is served by vite, on another origin.
ALLOW_ORIGINS = os.environ.get("RRR_ALLOW_ORIGINS", "*").split(",")

#: Whether the recording directory can be changed over HTTP. The path is only
#: checked for being a writable directory.
ALLOW_SETTINGS_WRITE = _flag("RRR_ALLOW_SETTINGS_WRITE", True)
