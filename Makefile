# Mac-side armv6 cross-build / push helpers (Docker on Mac, not on the Pi).
# Primary: make build && make push HOST=user@pi
# Fallback: make pack-venv HOST=user@pi && make push HOST=user@pi
.PHONY: build push pack-venv

build:
	./scripts/build-armv6.sh

push:
	./scripts/push-to-pi.sh $(HOST)

pack-venv:
	./scripts/pack-venv-from-pi.sh $(HOST)
