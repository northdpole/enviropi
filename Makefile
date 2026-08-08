# Mac-side armv6 cross-build / push helpers (Docker on Mac, not on the Pi).
# Day-to-day: make push          → local wheel only (fast)
# Deps/bootstrap: make build && make push-full
# HOST optional — defaults from .env ENVIROPI_HOST or ENVIROPI_SERVICE_USER@ENVIROPI_TAILSCALE_HOST
.PHONY: build push push-full pack-venv

build:
	./scripts/build-armv6.sh

push:
	./scripts/push-to-pi.sh --app $(HOST)

push-full:
	./scripts/push-to-pi.sh --full $(HOST)

pack-venv:
	./scripts/pack-venv-from-pi.sh $(HOST)
