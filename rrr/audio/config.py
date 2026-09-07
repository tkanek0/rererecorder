"""Runtime configuration for the core layer.

Every value can be overridden through the environment, so a second array or a
differently wired room needs no edit to the code.
"""

import os

# -- the device --------------------------------------------------------------

#: USB identity of the ReSpeaker USB Mic Array (XVF-3000). The tuning interface
#: is addressed by this rather than by ALSA card number, which changes between
#: plug-ins.
USB_VENDOR_ID = 0x2886
USB_PRODUCT_ID = 0x0018

#: Substring matched against PortAudio's device names. A name rather than an
#: index for the same reason: `hw:3` is whatever was plugged in last.
DEVICE_NAME = os.environ.get("RRR_AUDIO_DEVICE", "ReSpeaker")

#: The array's only capture rate. Fixed rather than negotiated so that a
#: processor's parameters mean the same thing across reconnects.
SAMPLE_RATE = 16000

#: The 6-channel firmware's layout. A 1-channel firmware would report one
#: channel here and break every index below, which is why the count is checked
#: at open time rather than assumed.
CHANNELS = 6

#: Channel 0 is what the XVF-3000 hands an ASR engine: beamformed towards the
#: detected direction, echo-cancelled, noise-suppressed and gain-controlled.
CHANNEL_PROCESSED = 0

#: Channels 1-4 are the raw microphones, in board order. These are the ones to
#: use for direction estimation of our own - the processed channel has already
#: thrown the spatial information away.
CHANNEL_MICS = (1, 2, 3, 4)

#: Channel 5 is a loopback of what was played out, provided so that echo
#: cancellation can be done downstream. Silent unless something is playing.
CHANNEL_PLAYBACK = 5

#: Where each microphone sits on the circle, in degrees, measured the same way
#: as the DOA angle the chip reports.
#:
#: NOT YET VERIFIED against the physical board. The four microphones are 90
#: degrees apart on a 70 mm circle, but which one is at zero depends on the
#: build configuration, and the datasheet says as much about DOAANGLE. Measure
#: with a known source before trusting the absolute offset; the spacing is
#: safe.
MIC_ANGLES = (45.0, 135.0, 225.0, 315.0)

#: Radius of the microphone circle in metres. Sets the maximum inter-microphone
#: delay, which bounds the search range of any time-difference estimator.
MIC_RADIUS_M = 0.0463

#: Speed of sound in metres per second, at about 20 degrees Celsius.
#:
#: It moves by 0.6 m/s per degree, which over a 46 mm array shifts the
#: inter-microphone delays by well under a sample at 16 kHz. Not worth making
#: configurable.
SPEED_OF_SOUND = 343.0

# -- capture -----------------------------------------------------------------

#: Frames per callback. 256 frames is 16 ms, small enough that streaming latency
#: is dominated by the browser's own buffer rather than by this, and large
#: enough not to spend the run in callback overhead.
BLOCK_SIZE = int(os.environ.get("RRR_AUDIO_BLOCK_SIZE", "256"))

#: Seconds of audio kept for analysis. Bounds both memory (10 s of 6 channels of
#: float32 is 3.8 MB) and how far back a processor can look.
WINDOW_S = float(os.environ.get("RRR_AUDIO_WINDOW_S", "10"))

#: How long capture keeps running after the last consumer goes away. Without
#: this, a series of one-shot reads would reopen the device every time, and the
#: first block after an open is tens of milliseconds away.
IDLE_SHUTDOWN_S = 10.0

#: Seconds to wait before reopening after the device disappears or errors.
RECONNECT_DELAY_S = 2.0

# -- direction of arrival ----------------------------------------------------

#: How often the chip is asked for its current angle.
#:
#: The value is a control transfer away, not a stream, so this is a genuine
#: poll - and an expensive one. Measured on this array, one transfer takes a
#: median of 16 ms, and a poll reads two of them (the angle and the voice
#: flag):
#:
#:   idle:                32 ms per poll  ->  31 Hz ceiling
#:   while capturing:     48 ms per poll  ->  21 Hz ceiling, p95 64 ms
#:
#: Audio capture is what makes the difference: its isochronous transfers have
#: priority on a full-speed bus, and control traffic waits behind them. Since
#: capture is running whenever anyone is listening, 15 Hz is the honest
#: default - its 67 ms period clears even the p95 - and asking for more only
#: produces an irregular rate rather than a faster one.
DOA_POLL_HZ = float(os.environ.get("RRR_AUDIO_DOA_POLL_HZ", "15"))

#: Seconds of angle history kept in memory, for drawing a trail behind the
#: current direction.
DOA_HISTORY_S = float(os.environ.get("RRR_AUDIO_DOA_HISTORY_S", "30"))

# -- output ------------------------------------------------------------------

#: Where the CLI writes recordings and logs.
OUTPUT_DIR = os.environ.get("RRR_AUDIO_OUTPUT_DIR", "var")
