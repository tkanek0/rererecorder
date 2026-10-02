"""The recorder's own code, under one name.

Layers run one way: ``rrr.timeline`` -> ``rrr.video`` -> ``rrr.recorder`` ->
``rrr.api``, ``rrr.tools``. Reading a recording back also starts at
``rrr.video``: ``rrr.inspection``, ``rrr.offset``, and ``rrr.playback`` ->
``rrr.visualization``. The array is reached through the separate
``respeaker_adapter`` package, which knows nothing of ``rrr``. Nothing is
imported eagerly, so ``import rrr`` needs no device SDK or web framework. See
docs/decisions.md 15 and docs/design.md "Module boundaries".
"""
