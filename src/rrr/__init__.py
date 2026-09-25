"""Everything this repository imports, under one name.

The packages below used to sit at the top level - ``audio``, ``video``,
``timeline``, ``recorder``, ``server``, ``tools``. Those are names any other
project might also use, so ``import video`` in a process that had this
repository on its path was a coin toss. Recordings made here are meant to be
read by other repositories, and a name collision at that boundary is the kind
of failure that looks like corrupted data.

``rrr`` matches what the rest of the repository already calls itself: the
``.rrdb`` archive suffix and the ``RRR_`` environment prefix.

The layer boundaries inside are unchanged and still run one way:

    rrr.timeline  ->  rrr.video, rrr.audio  ->  rrr.recorder  ->  rrr.server
                                                             ->  rrr.tools

Nothing here is imported eagerly. The subpackages pull in a device SDK, an
audio backend or a web framework, and importing ``rrr`` should not require any
of them - ``rrr.timeline`` in particular is meant to be testable on a machine
with no camera attached.
"""
