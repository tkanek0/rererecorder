"""Everything this repository imports, under one name.

Layers run one way: ``rrr.timeline`` -> ``rrr.video``, ``rrr.audio`` ->
``rrr.recorder`` -> ``rrr.api``, ``rrr.tools``. Nothing is imported eagerly,
so ``import rrr`` needs no device SDK or web framework. See docs/decisions.md
15 and docs/design.md "Module boundaries".
"""
