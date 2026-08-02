#!/usr/bin/env bash
# Pull a working .venv from the Pi into dist/ for fast later pushes (armv6l cache).
# Bootstrap/fallback when Docker cross-build is unavailable or mismatched.
# Usage: ./scripts/pack-venv-from-pi.sh [user@]host
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:-${ENVIROPI_HOST:-}}"
APP_REMOTE="${ENVIROPI_REMOTE_APP:-/opt/embedded-stack/apps/enviropi}"
OUT_DIR="${ENVIROPI_DIST:-$ROOT/dist}"
VENV_DIR="$OUT_DIR/venv-armv6"
TARBALL="$OUT_DIR/enviropi-venv-armv6l.tar.gz"

die() { printf 'pack-venv-from-pi: %s\n' "$*" >&2; exit 1; }
[[ -n "$HOST" ]] || die "missing host"

mkdir -p "$OUT_DIR"
rm -rf "$VENV_DIR"
mkdir -p "$VENV_DIR"

echo "pack-venv-from-pi: fetching ${HOST}:${APP_REMOTE}/.venv"
rsync -az --delete "${HOST}:${APP_REMOTE}/.venv/" "$VENV_DIR/"

arch="$(ssh -o BatchMode=yes "$HOST" 'uname -m')"
py="$(ssh -o BatchMode=yes "$HOST" "${APP_REMOTE}/.venv/bin/python -c 'import sys; print(sys.version)'")"
py_mm="$(ssh -o BatchMode=yes "$HOST" "${APP_REMOTE}/.venv/bin/python -c 'import sys; print(\"%d.%d\" % (sys.version_info[0], sys.version_info[1]))'")"
printf '%s\n' "$arch" >"$VENV_DIR/.enviropi-arch"
printf '%s\n' "$py" >"$VENV_DIR/.enviropi-python"
printf '%s\n' "$py_mm" >"$VENV_DIR/.enviropi-python-mm"
{
  echo "source=pi"
  echo "host=${HOST}"
  echo "arch=${arch}"
  echo "python=${py}"
  echo "python_mm=${py_mm}"
  echo "packed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$OUT_DIR/venv-armv6.meta"

tar -C "$OUT_DIR" -czf "$TARBALL" venv-armv6
echo "pack-venv-from-pi: wrote $TARBALL ($(du -h "$TARBALL" | awk '{print $1}'))"
echo "Next: ./scripts/push-to-pi.sh ${HOST}"
