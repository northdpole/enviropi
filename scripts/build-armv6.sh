#!/usr/bin/env bash
# Cross-build an armv6l (.venv) for Raspberry Pi Zero using Docker buildx + QEMU.
# Does NOT run on the Pi. Output: dist/venv-armv6/ and dist/enviropi-venv-armv6l.tar.gz
#
# Default base: Raspberry Pi OS Trixie armhf (Python 3.13) — matches current Pi Zero OS.
# Override: ENVIROPI_BASE_IMAGE=… ENVIROPI_DOCKER_PLATFORM=linux/arm/v6
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PLATFORM="${ENVIROPI_DOCKER_PLATFORM:-linux/arm/v6}"
BASE_IMAGE="${ENVIROPI_BASE_IMAGE:-vascoguita/raspios:armhf-trixie}"
QEMU_CPU="${ENVIROPI_QEMU_CPU:-arm1176}"
IMAGE_TAG="${ENVIROPI_IMAGE_TAG:-enviropi-armv6:venv}"
OUT_DIR="${ENVIROPI_DIST:-$ROOT/dist}"
# Primary artifact paths (push-to-pi.sh defaults here).
VENV_DIR="$OUT_DIR/venv-armv6"
TARBALL="$OUT_DIR/enviropi-venv-armv6l.tar.gz"
META="$OUT_DIR/venv-armv6.meta"

die() { printf 'build-armv6: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found"
docker buildx version >/dev/null 2>&1 || die "docker buildx required"

# Prefer colima socket when present
if [[ -z "${DOCKER_HOST:-}" ]]; then
  for s in "$HOME/.colima/default/docker.sock" "$HOME/.colima/docker.sock"; do
    if [[ -S "$s" ]]; then
      export DOCKER_HOST="unix://$s"
      break
    fi
  done
fi

echo "build-armv6: ensuring QEMU binfmt for ${PLATFORM}"
docker run --rm --privileged tonistiigi/binfmt --install arm >/dev/null

mkdir -p "$OUT_DIR"
# Stage into a temp dir so a failed build does not wipe a good cache (e.g. pack-venv-from-pi).
STAGE_DIR="$(mktemp -d "${OUT_DIR}/venv-armv6.staging.XXXXXX")"
cleanup_stage() { rm -rf "$STAGE_DIR"; }
trap cleanup_stage EXIT

echo "build-armv6: docker buildx (${PLATFORM}, base=${BASE_IMAGE})"
docker buildx build \
  --platform "$PLATFORM" \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "QEMU_CPU=${QEMU_CPU}" \
  -f docker/Dockerfile.armv6 \
  -t "$IMAGE_TAG" \
  --load \
  "$ROOT"

echo "build-armv6: extracting venv from image"
cid="$(docker create --platform "$PLATFORM" -e "QEMU_CPU=${QEMU_CPU}" "$IMAGE_TAG")"
docker cp "$cid:/venv/." "$STAGE_DIR/"
docker rm -f "$cid" >/dev/null

# Record metadata for push-time checks
py_mm=""
if [[ -f "$STAGE_DIR/.enviropi-python-mm" ]]; then
  py_mm="$(tr -d '[:space:]' <"$STAGE_DIR/.enviropi-python-mm")"
fi
{
  echo "source=docker"
  echo "platform=${PLATFORM}"
  echo "base_image=${BASE_IMAGE}"
  echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ -f "$STAGE_DIR/.enviropi-python" ]]; then
    echo "python=$(tr '\n' ' ' <"$STAGE_DIR/.enviropi-python" | sed 's/[[:space:]]*$//')"
  fi
  if [[ -n "$py_mm" ]]; then
    echo "python_mm=${py_mm}"
  fi
} >"${STAGE_DIR}.meta"

echo "build-armv6: publishing ${VENV_DIR}"
# macOS: mv into an existing dir fails with "Directory not empty"; swap via .old.
rm -rf "${VENV_DIR}.old"
if [[ -e "$VENV_DIR" ]]; then
  mv "$VENV_DIR" "${VENV_DIR}.old"
fi
mv "$STAGE_DIR" "$VENV_DIR"
mv -f "${STAGE_DIR}.meta" "$META"
rm -rf "${VENV_DIR}.old"
trap - EXIT

echo "build-armv6: packing ${TARBALL}"
tar -C "$OUT_DIR" -czf "$TARBALL" "$(basename "$VENV_DIR")"

echo "build-armv6: done"
echo "  venv:    $VENV_DIR"
echo "  tarball: $TARBALL"
echo "  meta:    $META"
if [[ -n "$py_mm" ]]; then
  echo "  python:  ${py_mm}"
fi
echo "Next: ./scripts/push-to-pi.sh user@pi"
echo "      (push refuses if venv Python minor ≠ Pi python3)"
