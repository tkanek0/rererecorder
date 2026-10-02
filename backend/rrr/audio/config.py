"""Runtime configuration for the array, each value overridable from the environment."""

import os

# -- the device --------------------------------------------------------------

#: USB identity of the ReSpeaker USB Mic Array (XVF-3000), stable across plug-ins
#: where the ALSA card number is not.
USB_VENDOR_ID = 0x2886
USB_PRODUCT_ID = 0x0018

#: Substring matched against PortAudio's device names, for the same reason.
DEVICE_NAME = os.environ.get("RRR_AUDIO_DEVICE", "ReSpeaker")

#: The array's only capture rate.
SAMPLE_RATE = 16000

#: The 6-channel firmware's layout, checked at open time.
CHANNELS = 6

#: Beamformed, echo-cancelled and gain-controlled; no spatial information left.
CHANNEL_PROCESSED = 0

#: The raw microphones, in board order.
CHANNEL_MICS = (1, 2, 3, 4)

#: Loopback of what was played out. Silent unless something is playing.
CHANNEL_PLAYBACK = 5

#: Where each microphone sits on the circle, in degrees, the way the chip's DOA
#: angle is measured. NOT YET VERIFIED: the spacing is safe, the zero is not.
MIC_ANGLES = (45.0, 135.0, 225.0, 315.0)

#: Radius of the microphone circle in metres.
MIC_RADIUS_M = 0.0463

#: Speed of sound in metres per second, at about 20 degrees Celsius. A degree
#: moves the delays by well under a sample, so it is not configurable.
SPEED_OF_SOUND = 343.0

# -- capture -----------------------------------------------------------------

#: Frames per callback: 16 ms.
BLOCK_SIZE = int(os.environ.get("RRR_AUDIO_BLOCK_SIZE", "256"))

#: Seconds of audio kept in memory for analysis.
WINDOW_S = float(os.environ.get("RRR_AUDIO_WINDOW_S", "10"))

#: How long capture keeps running after the last consumer goes away, so one-shot
#: reads do not reopen the device each time.
IDLE_SHUTDOWN_S = 10.0

# -- direction of arrival ----------------------------------------------------

#: How often the chip is polled for its angle. A poll is two control transfers,
#: measured at 48 ms (p95 64 ms) while capturing; 15 Hz clears that.
DOA_POLL_HZ = float(os.environ.get("RRR_AUDIO_DOA_POLL_HZ", "15"))

#: Seconds of angle history kept in memory.
DOA_HISTORY_S = float(os.environ.get("RRR_AUDIO_DOA_HISTORY_S", "30"))
