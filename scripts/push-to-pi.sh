#!/usr/bin/env bash
# Push EnviroPi app + prebuilt armv6 venv to a Pi over SSH/rsync (no heavy pip on Pi).
# Usage: ./scripts/push-to-pi.sh [user@]host
#
# Primary: Docker-built dist/venv-armv6 from ./scripts/build-armv6.sh
# Fallback: same paths filled by ./scripts/pack-venv-from-pi.sh (Pi-seeded cache)
# Refuses to install when venv Python minor ≠ remote python3 minor.
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

die() { printf 'push-to-pi: %s\n' "$*" >&2; exit 1; }

# SSH target: arg > ENVIROPI_HOST > SERVICE_USER@TAILSCALE_HOST
HOST="${1:-${ENVIROPI_HOST:-}}"
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

if [[ ! -d "$VENV_DIR/bin" && -f "$TARBALL" ]]; then
  echo "push-to-pi: extracting $TARBALL"
  mkdir -p "$OUT_DIR"
  tar -C "$OUT_DIR" -xzf "$TARBALL"
fi
[[ -x "$VENV_DIR/bin/python" || -x "$VENV_DIR/bin/python3" ]] \
  || die "missing prebuilt venv at ${VENV_DIR} (run ./scripts/build-armv6.sh or ./scripts/pack-venv-from-pi.sh)"

# --- Python minor must match remote system python3 ---
venv_mm=""
if [[ -f "$VENV_DIR/.enviropi-python-mm" ]]; then
  venv_mm="$(tr -d '[:space:]' <"$VENV_DIR/.enviropi-python-mm")"
elif [[ -f "$VENV_DIR/.enviropi-python" ]]; then
  # e.g. "3.13.5 (main, …)" → 3.13
  venv_mm="$(sed -n 's/^[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$VENV_DIR/.enviropi-python" | head -1)"
elif [[ -f "$VENV_DIR/pyvenv.cfg" ]]; then
  venv_mm="$(sed -n 's/^version_info[[:space:]]*=[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$VENV_DIR/pyvenv.cfg" | head -1)"
  if [[ -z "$venv_mm" ]]; then
    venv_mm="$(sed -n 's/^version[[:space:]]*=[[:space:]]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$VENV_DIR/pyvenv.cfg" | head -1)"
  fi
fi
[[ -n "$venv_mm" ]] || die "cannot determine venv Python version (missing .enviropi-python-mm); rebuild with ./scripts/build-armv6.sh"

remote_mm="$("$SSH_BIN" -o BatchMode=yes "$HOST" 'python3 -c "import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")"')"
remote_mm="$(tr -d '[:space:]' <<<"$remote_mm")"
[[ -n "$remote_mm" ]] || die "could not read remote python3 version on ${HOST}"

if [[ "$venv_mm" != "$remote_mm" ]]; then
  die "Python mismatch: venv=${venv_mm} remote=${remote_mm}. Refusing push.
  Rebuild for this Pi:  ./scripts/build-armv6.sh   # Trixie/3.13 armv6 default
  Or seed from Pi:      ./scripts/pack-venv-from-pi.sh ${HOST}
  Override paths only if you know they match: ENVIROPI_VENV_DIR=…"
fi
echo "push-to-pi: Python ${venv_mm} matches remote ${HOST}"

echo "push-to-pi: syncing project -> ${HOST}:${APP_REMOTE}"
"$SSH_BIN" "$HOST" "mkdir -p $(printf '%q' "$APP_REMOTE")/data"

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
  "$ROOT/" "${HOST}:${APP_REMOTE}/"

echo "push-to-pi: syncing prebuilt venv (tar stream; more reliable than rsync on Pi Zero)"
"$SSH_BIN" "$HOST" "rm -rf $(printf '%q' "$APP_REMOTE")/.venv.new && mkdir -p $(printf '%q' "$APP_REMOTE")/.venv.new"
# Stream a tarball so partial directory trees cannot race with --delete mkstemp failures.
tar -C "$VENV_DIR" -czf - . | "$SSH_BIN" "$HOST" \
  "tar -C $(printf '%q' "$APP_REMOTE")/.venv.new -xzf -"

# Remote activate: swap venv, rewrite shebangs, apt runtime libs, systemd
"$SSH_BIN" "$HOST" \
  "APP=$(printf '%q' "$APP_REMOTE") \
   SERVICE_USER=$(printf '%q' "$SERVICE_USER") \
   TAILSCALE_HOST=$(printf '%q' "$REMOTE_FQDN") \
   DASHBOARD_URL=$(printf '%q' "$DASHBOARD_URL") \
   OAUTH_REDIRECT_URI=$(printf '%q' "$OAUTH_REDIRECT_URI") \
   TELEGRAM_ALLOWLIST=$(printf '%q' "$TELEGRAM_ALLOWLIST") \
   WEB_PORT=$(printf '%q' "$WEB_PORT") \
   bash -s" <<'EOF'
set -euo pipefail
cd "$APP"

if [[ -d .venv ]]; then mv .venv ".venv.bak.$(date +%s)"; fi
mv .venv.new .venv

python3 - <<'PY'
from pathlib import Path
venv = Path(".venv").resolve()
bindir = venv / "bin"
py = bindir / "python3"
if not py.exists():
    py = bindir / "python"
# Use the venv python path (not .resolve() through a /usr/bin symlink), so
# pyvenv.cfg still activates site-packages when scripts run under systemd.
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

# Ensure the app package is importable in the swapped venv
./.venv/bin/python3 -m pip install -e . --no-deps -q
./.venv/bin/python3 -c "import enviropi; print('enviropi_ok', enviropi.__file__)"

# System shared libs used by numpy / sounddevice (not inside the venv)
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libopenblas0 libportaudio2 || true

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
    # Tailscale-reachable dashboard (UFW should allow only on tailscale0)
    "DASHBOARD_ENABLED": "true",
    "WEB_HOST": "0.0.0.0",
    "WEB_PORT": os.environ.get("WEB_PORT") or "8000",
    "OAUTH_REDIRECT_URI": os.environ["OAUTH_REDIRECT_URI"],
}
allow = (os.environ.get("TELEGRAM_ALLOWLIST") or "").strip()
if allow:
    updates["TELEGRAM_ALLOWLIST"] = allow
    # Keep alert chat id aligned when only allowlist was set from it
    if not (kv.get("TELEGRAM_ALERT_CHAT_ID") or "").strip():
        # First positive-looking id from allowlist
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
# dashboard_url / telegram_allowlist come from .env at runtime; keep YAML generic
python3 - <<'PY'
import os
import re
from pathlib import Path
p = Path("config.yaml")
text = p.read_text() if p.exists() else ""
url = os.environ["DASHBOARD_URL"]
# Keep a sensible YAML fallback matching deploy host (env still wins via get_config)
new = re.sub(
    r'(?m)^dashboard_url:\s*.*$',
    f'dashboard_url: "{url}"',
    text,
    count=1,
)
if new == text and "dashboard_url" not in text:
    new = f'dashboard_url: "{url}"\n' + text
# Scrub personal Telegram ids from committed-style YAML; use TELEGRAM_ALLOWLIST in .env
new2 = re.sub(
    r'(?m)^telegram_allowlist:\s*.*$',
    "telegram_allowlist: []",
    new,
    count=1,
)
if new2 == new and "telegram_allowlist" not in new:
    new2 = new.rstrip() + "\ntelegram_allowlist: []\n"
p.write_text(new2)
print("dashboard_url_ok", url)
print("telegram_allowlist_yaml_cleared")
PY
mkdir -p data

# UFW: dashboard only via Tailscale (idempotent; default deny keeps LAN/public closed)
if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  if ! sudo ufw status | grep -qE '8000/tcp.*tailscale0|Anywhere on tailscale0'; then
    sudo ufw allow in on tailscale0 to any port 8000 proto tcp || true
  fi
fi

sudo mkdir -p /opt/embedded-stack/systemd
sudo cp systemd/enviropi-*.service /opt/embedded-stack/systemd/
# Unit files ship as User=pi; rewrite for this host's service account
sudo sed -i \
  "s/^User=.*/User=${SERVICE_USER}/; s/^Group=.*/Group=${SERVICE_USER}/" \
  /opt/embedded-stack/systemd/enviropi-*.service
sudo cp /opt/embedded-stack/systemd/enviropi-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable enviropi-collector enviropi-web
sudo systemctl restart enviropi-collector
sudo systemctl restart enviropi-web
sleep 5
echo "collector=$(systemctl is-active enviropi-collector) web=$(systemctl is-active enviropi-web)"
echo "service_user=${SERVICE_USER}"
echo "listen:"; ss -ltnp 2>/dev/null | grep ':8000' || true
journalctl -u enviropi-web -n 20 --no-pager
journalctl -u enviropi-collector -n 15 --no-pager
EOF

echo "push-to-pi: done"
