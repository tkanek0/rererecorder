"""Facts about the array, and the defaults a caller may override by argument.

Nothing here reads the environment: the application that uses this package
decides how its settings are chosen and passes them in.
"""

# -- the device --------------------------------------------------------------

#: USB identity of the ReSpeaker USB Mic Array (XVF-3000), stable across plug-ins
#: where the ALSA card number is not.
USB_VENDOR_ID = 0x2886
USB_PRODUCT_ID = 0x0018

#: Substring matched against PortAudio's device names, for the same reason.
DEVICE_NAME = "ReSpeaker"

#: The array's only capture rate.
SAMPLE_RATE = 16000

#: The 6-channel firmware's layout, checked at open time.
CHANNELS = 6

#: Beamformed, echo-cancelled and gain-controlled; no spatial information left.
CHANNEL_PROCESSED = 0

#: The raw microphones, in board order.
CHANNEL_MICS = (1, 2, 3, 4)

# -- capture -----------------------------------------------------------------

#: Frames per callback: 16 ms.
BLOCK_SIZE = 256

# -- direction of arrival ----------------------------------------------------

#: How often the chip is polled for its angle. A poll is two control transfers,
#: measured at 48 ms (p95 64 ms) while capturing; 15 Hz clears that.
DOA_POLL_HZ = 15.0
