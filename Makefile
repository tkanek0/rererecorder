# Record a RealSense D455 - and, later, a ReSpeaker beside it.
#
#   make setup        Python deps, node deps
#   make check        tests (no device needed)
#
# The recorder runs two processes during development, each in its own terminal
# so its logs stay where you can read them:
#
#   make server       the control plane on :8040
#   make web          the page on :5177, talking to :8040
#
# Neither is needed to record. The CLI does it on its own:
#
#   make record       record a session   (SECONDS=10 SESSION=name)
#   make inspect      cross-check a recorded session   (DIR=var/sessions/x)
#   make devices      what the SDK can see
#
# The camera loses frames through the kernel's uvcvideo, so recording happens
# inside a container built on librealsense's RSUSB backend. Measured: 8.4% of
# depth frames lost through V4L2 at 1280x720, none through RSUSB.
#
#   make image        build the container
#   make drecord      record inside it   (this is the one to use)
#   make dserver      serve from inside it
#
# Ports are variables because 8000/8020/8030 and 5173/5175/5176 are taken on
# this machine by other projects.
#
#   make server PORT=8041
#   make web PORT=8041 WEB_PORT=5178

SECONDS   ?= 10
SESSION   ?=
DIR       ?=
DATA      ?= /mnt/dataspace02/rererecorder

#: Control plane port. Kept in step with web/src/lib/api.ts's default.
PORT      ?= 8040
#: Vite port. Pinned with strictPort in web/vite.config.ts.
WEB_PORT  ?= 5177

IMAGE     ?= rererecorder:latest
#: --user: recordings under var/ would otherwise come out owned by root.
#: --device: the RSUSB backend needs the USB bus and nothing else - no
#: /dev/video*, no kernel module, nothing privileged.
#: The audio group, because /dev/snd/* is root:audio 0660. On the host an ACL
#: lets the desktop user in; inside a container the ACL does not apply, so the
#: group has to be granted explicitly. Without it PortAudio simply reports no
#: devices - no error, just an empty list.
AUDIO_GID = $(shell getent group audio | cut -d: -f3)

#: --device /dev/bus/usb: the camera (RSUSB, libusb) and the array's direction
#: readout (a vendor control transfer) both need the USB bus. No /dev/video*,
#: no kernel module, nothing privileged.
#: --device /dev/snd: the array's audio, which goes through ALSA.
DOCKER_RUN = docker run --rm -it --user $(shell id -u):$(shell id -g) \
	--group-add $(AUDIO_GID) \
	--device /dev/bus/usb:/dev/bus/usb --device /dev/snd:/dev/snd \
	-v $(CURDIR):/app -v $(DATA):/data \
	-e RRR_SESSIONS_DIR=/data/sessions

.DEFAULT_GOAL := help
.PHONY: help setup check server web web-build record inspect devices \
        image drecord dserver clean

# Print the header above: from line 2 until the first line that is not a comment.
help:
	@sed -n '2,$${/^#/!q; s/^# \?//; p}' $(firstword $(MAKEFILE_LIST))

# -- setup -------------------------------------------------------------------

setup:
	uv sync
	cd web && npm install

check:
	uv run pytest

# -- development ------------------------------------------------------------

# Runs on the host, so it uses the V4L2 pyrealsense2 wheel and will lose
# frames. Fine for working on the page; use `make dserver` to record.
server:
	RRR_SERVER_PORT=$(PORT) uv run python -m server

web:
	@echo "opening on http://localhost:$(WEB_PORT), talking to :$(PORT)"
	cd web && VITE_CONTROL_URL=http://localhost:$(PORT) \
		npm run dev -- --port $(WEB_PORT)

# Build the page into the server, so one process serves everything.
web-build:
	cd web && npm run build

# -- the CLI ----------------------------------------------------------------

record:
	uv run python -m tools.record --seconds $(SECONDS) \
		$(if $(SESSION),--session $(SESSION),)

inspect:
	@test -n "$(DIR)" || (echo "usage: make inspect DIR=var/sessions/<name>"; exit 1)
	uv run python -m tools.inspect $(DIR)

devices:
	uv run python -c "from video import list_devices; \
		[print(d) for d in list_devices()]"

# -- the container ----------------------------------------------------------

image:
	docker build -f docker/Dockerfile -t $(IMAGE) .

drecord:
	$(DOCKER_RUN) $(IMAGE) python -m tools.record --seconds $(SECONDS) \
		$(if $(SESSION),--session $(SESSION),)

dserver:
	@echo "opening on http://localhost:$(PORT)"
	$(DOCKER_RUN) -p $(PORT):$(PORT) -e RRR_SERVER_PORT=$(PORT) $(IMAGE) \
		python -m server

# -- cleanup ----------------------------------------------------------------

clean:
	rm -rf var/sessions/* web/dist
