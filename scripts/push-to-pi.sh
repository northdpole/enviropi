#!/usr/bin/env bash
# Deploy EnviroPi to a Pi over SSH/rsync.
#
# Modes:
#   --app   (default) Build a pure-Python wheel locally, sync that + unit files,
#           pip install --no-deps on the Pi, restart services. Seconds, not minutes.
#   --full  Also replace the prebuilt armv6 venv (deps / Python / first install).
#
# Usage:
#   ./scripts/push-to-pi.sh [--app|--full] [user@]host
#
# Full-mode venv: dist/venv-armv6 from ./scripts/build-armv6.sh (or pack-venv-from-pi).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load local .env for deploy identity (does not override already-exported vars)
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

APP_REMOTE="${ENVIROPI_REMOTE_APP:-/opt/embedded-stack/apps/enviropi}"
OUT_DIR="${ENVIROPI_DIST:-$ROOT/dist}"
VENV_DIR="${ENVIROPI_VENV_DIR:-$OUT_DIR/venv-armv6}"
TARBALL="${ENVIROPI_VENV_TARBALL:-$OUT_DIR/enviropi-venv-armv6l.tar.gz}"
SSH_BIN="${ENVIROPI_SSH:-ssh}"
RSYNC_BIN="${ENVIROPI_RSYNC:-rsync}"
WEB_PORT="${WEB_PORT:-8000}"
MODE="${ENVIROPI_PUSH_MODE:-app}"

die() { printf 'push-to-pi: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./scripts/push-to-pi.sh [--app|--full] [user@]host

  --app   Fast path (default): local wheel → Pi install → restart
  --full  Slow path: sync prebuilt armv6 venv + app (deps / first install)

Host defaults from ENVIROPI_HOST / ENVIROPI_SERVICE_USER@ENVIROPI_TAILSCALE_HOST.
USAGE
}

HOST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) MODE=app; shift ;;
    --full) MODE=full; shift ;;
    -h|--help) usage; exit 0 ;;
    -*)
      die "unknown option: $1 (try --help)"
      ;;
    *)
      HOST="$1"
      shift
      ;;
  esac
done

# SSH target: arg > ENVIROPI_HOST > SERVICE_USER@TAILSCALE_HOST
HOST="${HOST:-${ENVIROPI_HOST:-}}"
if [[ -z "$HOST" && -n "${ENVIROPI_TAILSCALE_HOST:-}" ]]; then
  HOST="${ENVIROPI_SERVICE_USER:-pi}@${ENVIROPI_TAILSCALE_HOST}"
fi
[[ -n "$HOST" ]] || die "missing host. Set ENVIROPI_HOST / ENVIROPI_TAILSCALE_HOST in .env or: $0 user@host"
command -v "$RSYNC_BIN" >/dev/null || die "rsync not found"
command -v "$SSH_BIN" >/dev/null || die "ssh not found"

# Deploy-time identity (unit files ship as User=pi; override for this Pi)
if [[ "$HOST" == *@* ]]; then
  SERVICE_USER="${ENVIROPI_SERVICE_USER:-${HOST%%@*}}"
  REMOTE_FQDN="${ENVIROPI_TAILSCALE_HOST:-${HOST#*@}}"
else
  SERVICE_USER="${ENVIROPI_SERVICE_USER:-pi}"
  REMOTE_FQDN="${ENVIROPI_TAILSCALE_HOST:-$HOST}"
fi
DASHBOARD_URL="${ENVIROPI_DASHBOARD_URL:-http://${REMOTE_FQDN}:${WEB_PORT}}"
OAUTH_REDIRECT_URI="${ENVIROPI_OAUTH_REDIRECT_URI:-${OAUTH_REDIRECT_URI:-${DASHBOARD_URL%/}/auth/callback}}"
# Prefer deploy-derived OAuth URI so localhost leftovers in .env do not win on Pi
if [[ "$OAUTH_REDIRECT_URI" == *"127.0.0.1"* || "$OAUTH_REDIRECT_URI" == *"localhost"* ]]; then
  OAUTH_REDIRECT_URI="${DASHBOARD_URL%/}/auth/callback"
fi
TELEGRAM_ALLOWLIST="${TELEGRAM_ALLOWLIST:-${TELEGRAM_ALERT_CHAT_ID:-}}"
TELEGRAM_ALLOWLIST="$(printf '%s' "$TELEGRAM_ALLOWLIST" | tr -d '[:space:]')"

remote_ssh() {
  "$SSH_BIN" -o BatchMode=yes "$HOST" "$@"
}

ensure_remote_layout() {
  remote_ssh "mkdir -p $(printf '%q' "$APP_REMOTE")/data $(printf '%q' "$APP_REMOTE")/dist"
}

# --- Shared remote: env/config patch + systemd restart ---
# Args via env: APP, SERVICE_USER, TAILSCALE_HOST, DASHBOARD_URL, OAUTH_REDIRECT_URI,
# TELEGRAM_ALLOWLIST, WEB_PORT, MQTT_*, INSTALL_WHEEL (optional path), SKIP_APT (0/1), SWAP_VENV (0/1)
remote_activate() {
  local install_wheel="${1:-}"
  local skip_apt="${2:-1}"
  local swap_venv="${3:-0}"
  "$SSH_BIN" "$HOST" \
    "APP=$(printf '%q' "$APP_REMOTE") \
     SERVICE_USER=$(printf '%q' "$SERVICE_USER") \
     TAILSCALE_HOST=$(printf '%q' "$REMOTE_FQDN") \
     DASHBOARD_URL=$(printf '%q' "$DASHBOARD_URL") \
     OAUTH_REDIRECT_URI=$(printf '%q' "$OAUTH_REDIRECT_URI") \
     TELEGRAM_ALLOWLIST=$(printf '%q' "$TELEGRAM_ALLOWLIST") \
     WEB_PORT=$(printf '%q' "$WEB_PORT") \
     MQTT_HOST=$(printf '%q' "${MQTT_HOST:-homeserver.example.ts.net}") \
     MQTT_PORT=$(printf '%q' "${MQTT_PORT:-8883}") \
     MQTT_USERNAME=$(printf '%q' "${MQTT_USERNAME:-enviropi}") \
     MQTT_PASSWORD=$(printf '%q' "${MQTT_PASSWORD:-}") \
     MQTT_TOPIC=$(printf '%q' "${MQTT_TOPIC:-enviropi/enviroplus/state}") \
     MQTT_TLS=$(printf '%q' "${MQTT_TLS:-true}") \
     MQTT_TLS_INSECURE=$(printf '%q' "${MQTT_TLS_INSECURE:-false}") \
     INSTALL_WHEEL=$(printf '%q' "$install_wheel") \
     SKIP_APT=$(printf '%q' "$skip_apt") \
     SWAP_VENV=$(printf '%q' "$swap_venv") \
     bash -s" <<'EOF'
set -euo pipefail
cd "$APP"

if [[ "${SWAP_VENV}" == "1" ]]; then
  if [[ -d .venv ]]; then mv .venv ".venv.bak.$(date +%s)"; fi
  mv .venv.new .venv

  python3 - <<'PY'
from pathlib import Path
venv = Path(".venv").resolve()
bindir = venv / "bin"
py = bindir / "python3"
if not py.exists():
    py = bindir / "python"
shebang = f"#!{py}\n"
for path in bindir.iterdir():
    if not path.is_file() or path.is_symlink():
        continue
    try:
        raw = path.read_bytes()
    except OSError:
        continue
    if not raw.startswith(b"#!"):
        continue
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        continue
    nl = text.find("\n")
    if nl < 0:
        continue
    rest = text[nl + 1 :]
    path.write_text(shebang + rest)
    path.chmod(0o755)
print("shebangs_ok", py)
PY
fi

[[ -x .venv/bin/python3 || -x .venv/bin/python ]] \
  || { echo "push-to-pi: missing remote .venv — run with --full first" >&2; exit 1; }

if [[ -n "${INSTALL_WHEEL}" ]]; then
  ./.venv/bin/python3 -m pip install --no-deps --force-reinstall -q "${INSTALL_WHEEL}"
else
  ./.venv/bin/python3 -m pip install -e . --no-deps -q
fi
./.venv/bin/python3 -c "import enviropi; print('enviropi_ok', enviropi.__file__)"

if [[ "${SKIP_APT}" != "1" ]]; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libopenblas0 libportaudio2 || true
fi

if [[ ! -f .env ]]; then cp .env.example .env; fi
python3 - <<'PY'
import os
from pathlib import Path
p = Path(".env")
lines = p.read_text().splitlines()
kv, order = {}, []
for line in lines:
    if not line.strip() or line.strip().startswith("#") or "=" not in line:
        order.append(("raw", line))
        continue
    k, _, v = line.partition("=")
    kv[k] = v
    order.append(("kv", k))
updates = {
    "ENVIROPI_MOCK_SENSORS": "false",
    "ENVIROPI_DB": "/opt/embedded-stack/apps/enviropi/data/enviropi.db",
    "ENVIROPI_SERVICE_USER": os.environ["SERVICE_USER"],
    "ENVIROPI_TAILSCALE_HOST": os.environ["TAILSCALE_HOST"],
    "ENVIROPI_DASHBOARD_URL": os.environ["DASHBOARD_URL"],
    "DISPLAY_ENABLED": "true",
    "DASHBOARD_ENABLED": "true",
    "WEB_HOST": "0.0.0.0",
    "WEB_PORT": os.environ.get("WEB_PORT") or "8000",
    "OAUTH_REDIRECT_URI": os.environ["OAUTH_REDIRECT_URI"],
    "MQTT_ENABLED": "true",
    "MQTT_HOST": os.environ.get("MQTT_HOST") or "homeserver.example.ts.net",
    "MQTT_PORT": os.environ.get("MQTT_PORT") or "8883",
    "MQTT_USERNAME": os.environ.get("MQTT_USERNAME") or "enviropi",
    "MQTT_TOPIC": os.environ.get("MQTT_TOPIC") or "enviropi/enviroplus/state",
    "MQTT_TLS": os.environ.get("MQTT_TLS") or "true",
    "MQTT_TLS_INSECURE": os.environ.get("MQTT_TLS_INSECURE") or "false",
}
mqtt_pw = (os.environ.get("MQTT_PASSWORD") or "").strip()
if mqtt_pw:
    updates["MQTT_PASSWORD"] = mqtt_pw
elif not (kv.get("MQTT_PASSWORD") or "").strip():
    updates["MQTT_PASSWORD"] = ""
allow = (os.environ.get("TELEGRAM_ALLOWLIST") or "").strip()
if allow:
    updates["TELEGRAM_ALLOWLIST"] = allow
    if not (kv.get("TELEGRAM_ALERT_CHAT_ID") or "").strip():
        first = allow.split(",")[0].strip()
        if first.lstrip("-").isdigit() and not first.startswith("-"):
            updates["TELEGRAM_ALERT_CHAT_ID"] = first
for k, v in updates.items():
    if k not in kv:
        order.append(("kv", k))
    kv[k] = v
out, seen = [], set()
for kind, val in order:
    if kind == "raw":
        out.append(val)
    elif val not in seen:
        seen.add(val)
        out.append(f"{val}={kv[val]}")
p.write_text("\n".join(out) + "\n")
print("env_flags_ok")
PY

if [[ ! -f config.yaml && -f config.example.yaml ]]; then
  cp config.example.yaml config.yaml
fi
python3 - <<'PY'
import os
import re
from pathlib import Path

p = Path("config.yaml")
text = p.read_text() if p.exists() else ""
example = Path("config.example.yaml")
ex = example.read_text() if example.exists() else ""

# Keep personal MagicDNS / dashboard identity in .env only — never bake into config.yaml
GENERIC_DASH = "http://127.0.0.1:8000"
new = re.sub(
    r'(?m)^dashboard_url:\s*.*$',
    f'dashboard_url: "{GENERIC_DASH}"',
    text,
    count=1,
)
if new == text and "dashboard_url" not in text:
    new = f'dashboard_url: "{GENERIC_DASH}"\n' + text

new2 = re.sub(
    r'(?m)^telegram_allowlist:\s*.*$',
    "telegram_allowlist: []",
    new,
    count=1,
)
if new2 == new and "telegram_allowlist" not in new:
    new2 = new.rstrip() + "\ntelegram_allowlist: []\n"

# Migrate noisy historical defaults that caused Telegram chatter in warm/dry rooms
def bump_default(src: str, key_block: str, field: str, old: str, new_val: str) -> str:
    # Only replace when still exactly the old shipped default
    pattern = rf'(?m)^({key_block}:\n(?:  .*\n)*?  {field}:\s*){re.escape(old)}\s*$'
    return re.sub(pattern, rf'\g<1>{new_val}', src, count=1)

new2 = bump_default(new2, "temperature", "high", "28.0", "33.0")
new2 = bump_default(new2, "humidity", "low", "30.0", "20.0")
new2 = bump_default(new2, "hysteresis", "temperature", "0.5", "1.5")
new2 = bump_default(new2, "hysteresis", "humidity", "2.0", "3.0")

# Ensure status_report + catastrophe blocks exist (older Pi configs predate them)
for block in ("status_report", "catastrophe"):
    if re.search(rf'(?m)^{block}:\s*$', new2):
        continue
    m = re.search(rf'(?ms)^{block}:\n(?:  .*\n)+', ex)
    if m:
        # Insert before gas: or temperature: section
        insert_at = re.search(r'(?m)^(gas:|temperature:)', new2)
        if insert_at:
            i = insert_at.start()
            new2 = new2[:i] + m.group(0) + "\n" + new2[i:]
        else:
            new2 = new2.rstrip() + "\n\n" + m.group(0)

p.write_text(new2)
print("dashboard_url_generic_ok")
print("telegram_allowlist_yaml_cleared")
print("alert_defaults_migrated")
PY
mkdir -p data

if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  if ! sudo ufw status | grep -qE '8000/tcp.*tailscale0|Anywhere on tailscale0'; then
    sudo ufw allow in on tailscale0 to any port 8000 proto tcp || true
  fi
fi

sudo mkdir -p /opt/embedded-stack/systemd
sudo cp systemd/enviropi-*.service systemd/enviropi-*.timer /opt/embedded-stack/systemd/
sudo sed -i \
  "s/^User=.*/User=${SERVICE_USER}/; s/^Group=.*/Group=${SERVICE_USER}/" \
  /opt/embedded-stack/systemd/enviropi-*.service
sudo cp /opt/embedded-stack/systemd/enviropi-*.service \
  /opt/embedded-stack/systemd/enviropi-*.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable enviropi-collector enviropi-web
# Timer only — do not start/restart the oneshot (that would reboot during deploy).
sudo systemctl enable --now enviropi-weekly-reboot.timer
sudo systemctl restart enviropi-collector
sudo systemctl restart enviropi-web
sleep 3
echo "collector=$(systemctl is-active enviropi-collector) web=$(systemctl is-active enviropi-web)"
echo "reboot_timer=$(systemctl is-enabled enviropi-weekly-reboot.timer) next=$(systemctl show enviropi-weekly-reboot.timer -p NextElapseUSecRealtime --value)"
echo "service_user=${SERVICE_USER}"
echo "listen:"; ss -ltnp 2>/dev/null | grep ':8000' || true
journalctl -u enviropi-web -n 12 --no-pager
journalctl -u enviropi-collector -n 12 --no-pager
EOF
}

build_local_wheel() {
  local py=""
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    py="$ROOT/.venv/bin/python"
  elif [[ -x "$ROOT/.venv/bin/python3" ]]; then
    py="$ROOT/.venv/bin/python3"
  else
    py="$(command -v python3)"
  fi
  [[ -n "$py" ]] || die "python3 not found for local wheel build"
  # Status to stderr — stdout must be only the wheel path (rsync treats ":" as remote).
  echo "push-to-pi: building wheel with $py" >&2
  mkdir -p "$OUT_DIR"
  rm -f "$OUT_DIR"/enviropi-*.whl
  # Keep armv6 venv tree; only clear prior wheels / local build junk under dist/
  "$py" -m pip install -q build >&2
  "$py" -m build --wheel --outdir "$OUT_DIR" >&2
  local wheel
  wheel="$(ls -1 "$OUT_DIR"/enviropi-*.whl 2>/dev/null | head -1 || true)"
  [[ -n "$wheel" && -f "$wheel" ]] || die "wheel build produced no enviropi-*.whl in ${OUT_DIR}"
  printf '%s\n' "$wheel"
}

push_app() {
  echo "push-to-pi: mode=app (local wheel → artifacts only)"
  ensure_remote_layout
  # Require an existing remote venv from a prior --full (or manual) install
  remote_ssh "test -x $(printf '%q' "$APP_REMOTE")/.venv/bin/python3 -o -x $(printf '%q' "$APP_REMOTE")/.venv/bin/python" \
    || die "remote .venv missing on ${HOST}. First install: $0 --full ${HOST}"

  local wheel
  wheel="$(build_local_wheel)"
  local wheel_name
  wheel_name="$(basename "$wheel")"
  echo "push-to-pi: syncing ${wheel_name} + unit/config stubs"

  "$RSYNC_BIN" -az \
    "$wheel" \
    "${HOST}:${APP_REMOTE}/dist/"

  "$RSYNC_BIN" -az \
    "$ROOT/systemd/" \
    "${HOST}:${APP_REMOTE}/systemd/"

  "$RSYNC_BIN" -az \
    "$ROOT/config.example.yaml" \
    "$ROOT/.env.example" \
    "$ROOT/pyproject.toml" \
    "${HOST}:${APP_REMOTE}/"

  remote_activate "${APP_REMOTE}/dist/${wheel_name}" 1 0
}

push_full() {
  echo "push-to-pi: mode=full (venv + tree)"
  if [[ ! -d "$VENV_DIR/bin" && -f "$TARBALL" ]]; then
    echo "push-to-pi: extracting $TARBALL"
    mkdir -p "$OUT_DIR"
    tar -C "$OUT_DIR" -xzf "$TARBALL"
  fi
  [[ -x "$VENV_DIR/bin/python" || -x "$VENV_DIR/bin/python3" ]] \
    || die "missing prebuilt venv at ${VENV_DIR} (run ./scripts/build-armv6.sh or ./scripts/pack-venv-from-pi.sh)"

  local venv_mm=""
  if [[ -f "$VENV_DIR/.enviropi-python-mm" ]]; then
    venv_mm="$(tr -d '[:space:]' <"$VENV_DIR/.enviropi-python-mm")"
  elif [[ -f "$VENV_DIR/.enviropi-python" ]]; then
    venv_mm="$(sed -n 's/^[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$VENV_DIR/.enviropi-python" | head -1)"
  elif [[ -f "$VENV_DIR/pyvenv.cfg" ]]; then
    venv_mm="$(sed -n 's/^version_info[[:space:]]*=[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$VENV_DIR/pyvenv.cfg" | head -1)"
    if [[ -z "$venv_mm" ]]; then
      venv_mm="$(sed -n 's/^version[[:space:]]*=[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$VENV_DIR/pyvenv.cfg" | head -1)"
    fi
  fi
  [[ -n "$venv_mm" ]] || die "cannot determine venv Python version (missing .enviropi-python-mm); rebuild with ./scripts/build-armv6.sh"

  local remote_mm
  remote_mm="$(remote_ssh 'python3 -c "import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")"')"
  remote_mm="$(tr -d '[:space:]' <<<"$remote_mm")"
  [[ -n "$remote_mm" ]] || die "could not read remote python3 version on ${HOST}"

  if [[ "$venv_mm" != "$remote_mm" ]]; then
    die "Python mismatch: venv=${venv_mm} remote=${remote_mm}. Refusing push.
  Rebuild for this Pi:  ./scripts/build-armv6.sh
  Or seed from Pi:      ./scripts/pack-venv-from-pi.sh ${HOST}"
  fi
  echo "push-to-pi: Python ${venv_mm} matches remote ${HOST}"

  ensure_remote_layout

  echo "push-to-pi: syncing project -> ${HOST}:${APP_REMOTE}"
  "$RSYNC_BIN" -az --delete \
    --exclude '.venv/' \
    --exclude '.venv.new/' \
    --exclude '.venv-armv6/' \
    --exclude '.git/' \
    --exclude 'data/' \
    --exclude 'dist/' \
    --exclude '.pytest_cache/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.DS_Store' \
    --exclude '.env' \
    --exclude 'config.yaml' \
    "$ROOT/" "${HOST}:${APP_REMOTE}/"

  echo "push-to-pi: syncing prebuilt venv (tar stream)"
  remote_ssh "rm -rf $(printf '%q' "$APP_REMOTE")/.venv.new && mkdir -p $(printf '%q' "$APP_REMOTE")/.venv.new"
  tar -C "$VENV_DIR" -czf - . | "$SSH_BIN" "$HOST" \
    "tar -C $(printf '%q' "$APP_REMOTE")/.venv.new -xzf -"

  local wheel
  wheel="$(build_local_wheel)"
  local wheel_name
  wheel_name="$(basename "$wheel")"
  "$RSYNC_BIN" -az "$wheel" "${HOST}:${APP_REMOTE}/dist/"
  remote_activate "${APP_REMOTE}/dist/${wheel_name}" 0 1
}

case "$MODE" in
  app) push_app ;;
  full) push_full ;;
  *) die "unknown mode: ${MODE}" ;;
esac

echo "push-to-pi: done (${MODE})"
