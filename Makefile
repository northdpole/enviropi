# Mac-side armv6 cross-build / push helpers (Docker on Mac, not on the Pi).
# Primary: make build && make push
# HOST optional — defaults from .env ENVIROPI_HOST or ENVIROPI_SERVICE_USER@ENVIROPI_TAILSCALE_HOST
# Fallback: make pack-venv && make push
.PHONY: build push pack-venv

build:
	./scripts/build-armv6.sh

push:
	./scripts/push-to-pi.sh $(HOST)

pack-venv:
	./scripts/pack-venv-from-pi.sh $(HOST)
