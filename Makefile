# rererecorder - launching the recorder. Details: README.md, docs/.
#
#   make setup      deps
#   make server     control plane on the host (drops frames; for page work)
#   make web        the page, talking to the control plane
#   make image      build the container
#   make dserver    control plane in the container - the way to record
#
#   make server PORT=8041
#   make web PORT=8041 WEB_PORT=5178

PORT     ?= 8040
WEB_PORT ?= 5177

# docker/compose.yaml takes these from the environment; see docs/decisions.md 13.
COMPOSE = UID=$(shell id -u) GID=$(shell id -g) \
	AUDIO_GID=$(shell getent group audio | cut -d: -f3) RRR_SERVER_PORT=$(PORT) \
	docker compose -f docker/compose.yaml

.DEFAULT_GOAL := help
.PHONY: help setup server web image dserver

help:
	@sed -n '2,$${/^#/!q; s/^# \?//; p}' $(firstword $(MAKEFILE_LIST))

setup:
	uv sync
	cd web && npm install

server:
	RRR_SERVER_PORT=$(PORT) uv run python -m rrr.server

web:
	cd web && VITE_CONTROL_URL=http://localhost:$(PORT) npm run dev -- --port $(WEB_PORT)

image:
	$(COMPOSE) build

dserver:
	$(COMPOSE) run --rm --service-ports recorder
