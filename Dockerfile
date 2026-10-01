# librealsense built for the RSUSB backend, which the PyPI wheel (V4L2) is not.
# See docs/decisions.md 1.

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

# Examples off: the viewer drags in GL and GTK, and nothing here displays.
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

# -- runtime stage ------------------------------------------------------------
FROM python:3.12-slim-bookworm

# libgomp for the OpenCV wheel; libportaudio2 because sounddevice loads it on
# import.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libusb-1.0-0 libgomp1 libportaudio2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

COPY --from=librealsense /usr/local/lib/librealsense2.so* /usr/local/lib/
COPY --from=librealsense /usr/local/lib/python3.12/ /usr/local/lib/python3.12/
RUN ldconfig

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./

# --no-install-package pyrealsense2: keep the one built above, not the wheel.
ENV UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never
# Dependencies first, so this layer survives a source change.
RUN uv sync --frozen --no-install-package pyrealsense2 --no-install-project --inexact

COPY . .
# Editable, so the checkout bind-mounted over /app is what runs.
RUN uv sync --frozen --no-install-package pyrealsense2 --inexact

# Fail the build rather than fall back to V4L2 silently.
RUN python -c "import pyrealsense2 as rs; print('librealsense', rs.__version__)"

EXPOSE 8040
CMD ["python", "-m", "rrr.api"]
