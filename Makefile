# rererecorder - launching the recorder. Details: README.md, docs/.
#
#   make setup      deps
#   make up         api and page together on the host (drops frames; page work)
#   make api        control plane alone, on the host
#   make app        the page alone, talking to the control plane
#   make image      build the container
#   make dserver    control plane in the container - the way to record
#
#   make up HOST=127.0.0.1
#   make up API_PORT=8041 APP_PORT=5178

# Every interface, so the page and the control plane can both be reached from
# another machine - a recorder on a Pi is driven from a laptop.
HOST     ?= 0.0.0.0
API_PORT ?= 8040
APP_PORT ?= 5177

# `wait -n` below is bash, and make's default /bin/sh is dash here.
SHELL := /bin/bash

# docker/compose.yaml takes these from the environment; see docs/decisions.md 13.
COMPOSE = UID=$(shell id -u) GID=$(shell id -g) \
	AUDIO_GID=$(shell getent group audio | cut -d: -f3) RRR_API_PORT=$(API_PORT) \
	docker compose -f docker/compose.yaml

.DEFAULT_GOAL := help
.PHONY: help setup up api app image dserver

help:
	@sed -n '2,$${/^#/!q; s/^# \?//; p}' $(firstword $(MAKEFILE_LIST))

setup:
	uv sync
	cd web && npm install

# Both halves in one terminal, each killed by the PID noted when it started -
# no name matching, which would also hit an api someone else is running.
up:
	@RRR_API_HOST=$(HOST) RRR_API_PORT=$(API_PORT) uv run python -m rrr.api & api=$$!; \
	VITE_CONTROL_PORT=$(API_PORT) npm --prefix web run dev -- --host $(HOST) --port $(APP_PORT) & app=$$!; \
	trap 'kill $$api $$app 2>/dev/null' INT TERM EXIT; \
	wait -n; \
	kill $$api $$app 2>/dev/null

api:
	RRR_API_HOST=$(HOST) RRR_API_PORT=$(API_PORT) uv run python -m rrr.api

# No VITE_CONTROL_URL: the page derives the control plane's host from its own
# location, so opening it from another machine reaches that machine's api
# rather than the viewer's own localhost. Only the port has to be told.
app:
	VITE_CONTROL_PORT=$(API_PORT) npm --prefix web run dev -- --host $(HOST) --port $(APP_PORT)

image:
	$(COMPOSE) build

dserver:
	$(COMPOSE) run --rm --service-ports recorder
