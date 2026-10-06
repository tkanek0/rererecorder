"""The control plane: preview out, recording commands in.

Transport only; what a recording contains is decided in :mod:`rrr.recorder`
and :mod:`rrr.video`. One hub owns the camera and is shared by the preview and
the recording - see docs/design.md "The camera is shared".
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config, devices, live, recording, sessions, settings
from .state import state

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hold the devices for the life of the server."""
    logger.info("server starting on %s:%d", config.HOST, config.PORT)
    yield
    state.close()


app = FastAPI(title="rererecorder", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    # The page is cross-origin; unlisted headers are hidden from the player.
    expose_headers=["X-Frame-Index", "X-Received-Monotonic"],
)
for module in (devices, recording, sessions, settings, live):
    app.include_router(module.router)
