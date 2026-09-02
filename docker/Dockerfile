# librealsense built for the RSUSB backend, and this repository on top of it.
#
# The backend is the whole reason this image exists. The pyrealsense2 wheel on
# PyPI is built against V4L2, and V4L2 loses frames on this camera: measured on
# a D455 at 1280x720 depth plus 1280x800 colour, 8.4% of depth frames and 9.6%
# of colour frames never reach the process, while the camera goes on numbering
# them at 30/s. The same test through RSUSB - libusb in user space, no uvcvideo -
# loses none, at 172 MB/s with both raw infrared streams added on top.
#
# So librealsense is compiled here with FORCE_RSUSB_BACKEND and its Python
# bindings, and the wheel's build is replaced by it.

# -- build stage --------------------------------------------------------------
FROM python:3.12-slim-bookworm AS librealsense

ARG LIBREALSENSE_VERSION=v2.58.3
ARG BUILD_JOBS=

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config \
        libusb-1.0-0-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch ${LIBREALSENSE_VERSION} \
        https://github.com/IntelRealSense/librealsense.git

# FORCE_RSUSB_BACKEND: user-space libusb rather than the kernel's uvcvideo. It
# also means no kernel patch and no v4l dependency, which is what makes this
# image portable to a Raspberry Pi.
# BUILD_EXAMPLES/BUILD_GRAPHICAL_EXAMPLES off: this image records, it does not
# display, and the viewer drags in GL and GTK.
RUN cmake -S librealsense -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DFORCE_RSUSB_BACKEND=true \
        -DBUILD_PYTHON_BINDINGS=true \
        -DPYTHON_EXECUTABLE=/usr/local/bin/python3.12 \
        -DBUILD_EXAMPLES=false \
        -DBUILD_GRAPHICAL_EXAMPLES=false \
        -DBUILD_UNIT_TESTS=false \
        -DBUILD_TOOLS=false \
    && cmake --build build --parallel ${BUILD_JOBS:-$(nproc)} \
    && cmake --install build

# -- frontend stage -----------------------------------------------------------
FROM node:24-slim AS web

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# Built here so the runtime image needs no node at all: what ships is a few
# hundred kilobytes of static files, and a Raspberry Pi never compiles them.
RUN npm run build

# -- runtime stage ------------------------------------------------------------
FROM python:3.12-slim-bookworm

# libusb for the camera; libgomp is what the OpenCV wheel links against;
# libportaudio2 is loaded at import time by sounddevice, so the audio package
# cannot even be imported without it - and recorder/ imports it unconditionally.
# No v4l-utils, no uvcvideo: the RSUSB backend talks to /dev/bus/usb directly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libusb-1.0-0 libgomp1 libportaudio2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

COPY --from=librealsense /usr/local/lib/librealsense2.so* /usr/local/lib/
COPY --from=librealsense /usr/local/lib/python3.12/ /usr/local/lib/python3.12/
RUN ldconfig

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./

# --no-install-package pyrealsense2: the lock file names the PyPI wheel, which
# is the V4L2 build this image exists to avoid. The version installed above is
# the same 2.58.3, compiled differently.
ENV UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-install-package pyrealsense2 --inexact

COPY . .
# After COPY, so a stale local web/dist cannot shadow the one just built.
COPY --from=web /web/dist ./web/dist

# Fail loudly at start if the wrong backend somehow ended up in the image: a
# silent fallback to V4L2 would look like a working recorder that drops 8% of
# its frames.
RUN python -c "import pyrealsense2 as rs; print('librealsense', rs.__version__)"

ENV PYTHONPATH=/app
EXPOSE 8040
# The server rather than the CLI: the image is the recorder, and the recorder
# has a page. `docker run ... python -m tools.record` still works.
CMD ["python", "-m", "server"]
