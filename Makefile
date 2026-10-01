# rererecorder on Linux. See README.md.
#
#   make setup      host deps, for tests and the tools that read recordings
#   make image      build the recorder image
#   make up         control plane and page, both in containers
#
#   make up HOST=127.0.0.1
#   make up API_PORT=8041 APP_PORT=5178

HOST     ?= 0.0.0.0
API_PORT ?= 8040
APP_PORT ?= 5177

# See docs/decisions.md 13 for the ids.
COMPOSE = UID=$(shell id -u) GID=$(shell id -g) \
	AUDIO_GID=$(shell getent group audio | cut -d: -f3) \
	RRR_HOST=$(HOST) RRR_API_PORT=$(API_PORT) RRR_APP_PORT=$(APP_PORT) \
	docker compose

.DEFAULT_GOAL := help
.PHONY: help setup image up

help:
	@sed -n '2,$${/^#/!q; s/^# \?//; p}' $(firstword $(MAKEFILE_LIST))

setup:
	uv sync
	cd web && npm install

image:
	$(COMPOSE) build

up:
	$(COMPOSE) up

