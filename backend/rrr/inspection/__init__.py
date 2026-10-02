"""Cross-checking a recorded session's files against each other.

Re-reads what was written and makes the files argue, rather than repeating what
the recorder believed. How the findings are shown belongs to the caller: see
``scripts/inspect_session.py``.
"""

from .checks import Check, Inspection, count_missing, inspect_session

__all__ = ["Check", "Inspection", "count_missing", "inspect_session"]
