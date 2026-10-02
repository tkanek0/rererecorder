"""Run the control plane.

    uv run python -m rrr.api
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
        # The object, not an import string: no module lookup is needed, and
        # only --reload or multiple workers would require the string form.
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        # Finite: MJPEG responses never end on their own, and a server that
        # will not stop keeps the camera held.
        timeout_graceful_shutdown=config.SHUTDOWN_TIMEOUT_S,
    )


if __name__ == "__main__":
    main()
