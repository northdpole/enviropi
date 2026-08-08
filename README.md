# EnviroPi

Monitor an **Enviro+** (no particulate sensor required) on a Raspberry Pi Zero: store a year of history, show a Google-OAuth dashboard, and send Telegram alerts on configurable conditions.

## Features

- Polls temperature (CPU-compensated), humidity, pressure, lux, noise, and MICS6814 gases (reducing / oxidising / NH3)
- Enviro+ LCD wakes on proximity (look/near), shows latest readings, then sleeps — not permanently on
- SQLite storage: raw samples for 14 days, hourly rollups kept for a year+
- Telegram: scheduled status digests (customizable via `/digest`) with period highs/lows, edge-triggered limit alerts (breach + resolve), catastrophe spike alerts; `/help`, `/status`, `/alerts`, `/digest`, `/set`, `/reset`, `/mute`, `/unmute`
- Dashboard charts (24h / 7d / 30d / 1y) and settings UI
- Threshold defaults in `config.yaml`; overrides from Telegram **or** dashboard (shared SQLite)
- Google OAuth2 with email allowlist

## Quick start (dev / mock sensors)

```bash
cd enviropi
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
cp .env.example .env
# Edit .env — for local smoke test, mock sensors stay true

mkdir -p data
enviropi-collector   # terminal 1
enviropi-web         # terminal 2
```

Open `http://127.0.0.1:8000/healthz` (no auth). Dashboard requires Google OAuth.

### Off-Pi without OAuth

You can hit `/healthz` and run the collector with mocks. Chart APIs require a logged-in session.

## Raspberry Pi Zero + Enviro+

Pi Zero is **armv6l / armhf**. Prefer building the Python venv on your Mac in an emulated
`linux/arm/v6` container, then pushing it — the Pi should not compile wheels.

### Fast path (Mac Docker → Pi over Tailscale)

Prereqs on the Mac: Docker/Colima, `docker buildx`, QEMU binfmt (`tonistiigi/binfmt`).

```bash
# One-time on Mac (Colima): register arm emulators
docker run --rm --privileged tonistiigi/binfmt --install arm

# Primary: emulated armv6 build (Raspberry Pi OS Trixie / Python 3.13)
make build
# or: ./scripts/build-armv6.sh
# → dist/venv-armv6/ + dist/enviropi-venv-armv6l.tar.gz

# Push code + prebuilt venv (no Docker / heavy pip on Pi)
./scripts/push-to-pi.sh user@enviropi.example.ts.net
# or: make push HOST=user@enviropi.example.ts.net
```

`push-to-pi.sh` compares the venv’s Python minor to remote `python3` and **refuses** a
mismatch (e.g. old 3.11 bookworm artifact vs Pi Trixie 3.13).

**Fallback** if QEMU build is impractical: seed once from a working Pi, then push:

```bash
./scripts/pack-venv-from-pi.sh user@enviropi.example.ts.net
./scripts/push-to-pi.sh user@enviropi.example.ts.net
```

**Base image:** `vascoguita/raspios:armhf-trixie` (`linux/arm/v6`). Override with
`ENVIROPI_BASE_IMAGE` if needed. Official `python:3.13-slim` has no arm/v6; balena
images stop at bookworm (3.11).

**One-time apt on the Pi** (shared libs, not inside the venv):

```bash
sudo apt-get install -y libopenblas0 libportaudio2
# Enable I2C + SPI (raspi-config), then reboot if /dev/spidev* missing
```

### Manual / first-time layout

1. Install Raspberry Pi OS, enable I2C/SPI.
2. Create embedded-stack dirs (or run laptop `enviropi-deploy --run` for hardening bootstrap):

   ```bash
   sudo mkdir -p /opt/embedded-stack/apps /opt/embedded-stack/systemd
   ```

3. Prefer `./scripts/push-to-pi.sh` above. Manual alternative:

   ```bash
   rsync -a --delete --exclude .venv --exclude .git --exclude data --exclude .env \
     ./ user@pi:/opt/embedded-stack/apps/enviropi/
   ```

4. Copy and edit config:

   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```

   Set in `.env` (personal identity stays here — not in committed `config.yaml`):

   - `ENVIROPI_SERVICE_USER` (systemd `User=`/`Group=`; `push-to-pi.sh` rewrites units)
   - `ENVIROPI_TAILSCALE_HOST` (MagicDNS host → `dashboard_url` + OAuth redirect base)
   - `ENVIROPI_MOCK_SENSORS=false`
   - `DISPLAY_ENABLED=true` (proximity-wake LCD; `false` leaves backlight alone)
   - `DASHBOARD_ENABLED=true` (or `false` to run collector only)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`, optional `TELEGRAM_ALLOWLIST`
   - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`, `OAUTH_ALLOWLIST`
   - `SESSION_SECRET` (long random string)
   - `ENVIROPI_DB=/opt/embedded-stack/apps/enviropi/data/enviropi.db`

5. Leave `telegram_allowlist: []` and a generic `dashboard_url` in `config.yaml`;
   runtime merges `TELEGRAM_ALLOWLIST` / private `TELEGRAM_ALERT_CHAT_ID` and
   `ENVIROPI_TAILSCALE_HOST` (or `ENVIROPI_DASHBOARD_URL`) from `.env`.

6. Install systemd units (drafts live under `/opt/embedded-stack/systemd/`):

   ```bash
   sudo cp systemd/enviropi-*.service /opt/embedded-stack/systemd/
   sudo cp /opt/embedded-stack/systemd/enviropi-*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now enviropi-collector enviropi-web
   ```

   Units default to `User=pi` / `Group=pi` with
   `WorkingDirectory=/opt/embedded-stack/apps/enviropi`. Override at deploy time
   (`ENVIROPI_SERVICE_USER` or the SSH user via `push-to-pi.sh`) if the Pi account differs.
   Bind with `WEB_HOST=0.0.0.0` and keep UFW default-deny; allow the dashboard only on the
   tailnet interface (homeserver pattern — not LAN/public):

   ```bash
   sudo ufw allow in on tailscale0 to any port 8000 proto tcp
   ```

   Health check from another tailnet device:
   `http://enviropi.example.ts.net:8000/healthz`

## Google OAuth (Tailscale)

Google requires a registered redirect URI matching `OAUTH_REDIRECT_URI`.

1. Create a Google Cloud **OAuth 2.0 Client ID** (Web application).
2. Authorized redirect URI: `http://enviropi.example.ts.net:8000/auth/callback`
   (or HTTPS if you put a tunnel/proxy in front).
3. Put the client in **Testing** mode and add your Google account as a test user, or publish the app.
4. Set `OAUTH_ALLOWLIST=you@gmail.com` (comma-separated).
5. On the Pi, bind all interfaces (`WEB_HOST=0.0.0.0`) and restrict exposure with UFW on
   `tailscale0` only (see above).

## Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather); put the token in `.env`.
2. Open a chat with the bot and send any message (required before alerts can be delivered).
3. Find your **numeric user id** (e.g. `@userinfobot` or `getUpdates`) → `TELEGRAM_ALERT_CHAT_ID`
   and optionally `TELEGRAM_ALLOWLIST` (comma-separated). Prefer `.env` over `config.yaml`.
   In a private chat this id equals your user id. Group chat ids are negative and only receive alerts.
4. Empty YAML allowlist does **not** open the bot: a positive `TELEGRAM_ALERT_CHAT_ID` (private chat)
   and/or `TELEGRAM_ALLOWLIST` authorize commands. Everyone else gets `Unauthorized.`
   Alerts are only sent to `TELEGRAM_ALERT_CHAT_ID`.

Commands (owner allowlist / private alert recipient only):

| Command | Action |
|---------|--------|
| `/help` | List commands |
| `/status` | Latest readings with low/hi threshold refs |
| `/alerts` | Effective thresholds |
| `/digest` | Show/set status report times (`/digest 08:00 20:00`) |
| `/set <key> <value>` | Override threshold |
| `/reset <key>` | Clear override |
| `/mute [hours]` | Silence alerts |
| `/unmute` | Resume alerts |

Example keys: `temperature.high`, `humidity.low`, `gas_reducing.high`, `status_report.times`.

### Gas thresholds

MICS6814 reports **resistance (Ω)**, not ppm:

- **Reducing / NH3**: resistance **falls** as those gases rise → set `gas_*.high` as a **floor**; alert fires when reading goes **below** it.
- **Oxidising**: resistance **rises** with NO₂-like gases → alert when reading goes **above** `gas_oxidising.high`.

Leave gas thresholds `null` until you have a stable baseline (sensor needs warm-up; see `gas.baseline_warmup_min`). Optional `gas.relative_change_pct` alerts on % move vs a rolling baseline after warm-up.

## Alert behaviour

| Kind | Behaviour |
|------|-----------|
| Status report | At configured local times (`/digest` or `status_report.times`) with current readings plus highs/lows since the previous digest |
| Limit breach | One Telegram message when a threshold is crossed |
| Limit resolve | One message when the reading returns inside the limit (with hysteresis) |
| Catastrophe | Immediate message on a sudden spike within `catastrophe.window_min` (fire / flood / extreme gas). Bypasses `/mute`. Repeat suppressed for `catastrophe.cooldown_sec` |

## Alert defaults

| Condition | Default |
|-----------|---------|
| Temp high / low | 28°C / 10°C |
| Humidity high / low | 70% / 30% |
| Pressure, gases, noise, lux | off (`null`) |
| Status report | 08:00 and 20:00 Europe/London |
| Catastrophe window | 5 min; +5°C / +25%RH / 35% gas swing |

## Layout

```
src/enviropi/
  collector.py      # poll + alerts + Telegram long-poll
  sensors.py        # Enviro+ or mock
  db.py             # SQLite
  alerts.py         # threshold evaluation
  telegram_bot.py
  config.py
  web/app.py        # FastAPI dashboard + OAuth
systemd/
config.example.yaml
.env.example
```

Set `DASHBOARD_ENABLED=false` in `.env` to skip the HTTP dashboard (`enviropi-web` exits cleanly) while `enviropi-collector` keeps running.

### Enviro+ LCD

The screen does **not** turn on by itself when you look at it. Pimoroni’s examples use the LTR-559 proximity sensor in software; EnviroPi does the same:

- Backlight starts **off**
- Coming near / looking at the sensor wakes it and shows latest T/H/P/lux
- After ~15s without proximity, backlight turns **off** again
- `DISPLAY_ENABLED=false` disables this entirely (collection still runs)
