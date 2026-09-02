"""Run the control plane.

    uv run python -m server
"""

from __future__ import annotations

import logging

import uvicorn

from . import config


def main() -> None:
    """Serve until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    uvicorn.run(
        "server.app:app",
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
