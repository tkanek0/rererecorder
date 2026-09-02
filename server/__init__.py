"""The control plane: HTTP and MJPEG around the recorder.

Depends on :mod:`recorder`, :mod:`video` and :mod:`timeline`, and adds nothing
of its own to what a recording contains. Nothing in those packages imports this
one, which is what keeps the CLI usable with no web stack installed.
"""
