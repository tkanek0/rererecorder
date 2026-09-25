"""Run the control plane.

    uv run python -m rrr.server
"""

from __future__ import annotations

import logging

import uvicorn

from . import config
from .app import app


def main() -> None:
    """Serve until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    uvicorn.run(
        # The app object itself, not the "server.app:app" import string: the
        # string form re-imports by module name, which needs `rrr/` itself -
        # not just the repository root - on sys.path to resolve the bare
        # `server` package, and nothing arranges that when this runs as
        # `python -m rrr.server`. Passing the object sidesteps the lookup
        # entirely; only `--reload` or multiple workers need the string form,
        # neither of which this uses.
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        # Finite: an MJPEG response ends only when its client disconnects, so
        # an unbounded graceful shutdown never finishes - and a server that will
        # not stop leaves the camera held.
        timeout_graceful_shutdown=config.SHUTDOWN_TIMEOUT_S,
    )


if __name__ == "__main__":
    main()
