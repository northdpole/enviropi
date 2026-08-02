# EnviroPi

Monitor an **Enviro+** (no particulate sensor required) on a Raspberry Pi Zero: store a year of history, show a Google-OAuth dashboard, and send Telegram alerts on configurable conditions.

## Features

- Polls temperature (CPU-compensated), humidity, pressure, lux, noise, and MICS6814 gases (reducing / oxidising / NH3)
- SQLite storage: raw samples for 14 days, hourly rollups kept for a year+
- Telegram alerts with cooldown/hysteresis; `/status`, `/alerts`, `/set`, `/reset`, `/mute`, `/unmute`
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

1. Install Raspberry Pi OS Bookworm, enable I2C/SPI (Enviro+ installer does this).
2. Install Pimoroni Enviro+ library:

   ```bash
   git clone https://github.com/pimoroni/enviroplus-python
   cd enviroplus-python && ./install.sh
   sudo reboot
   ```

3. Clone this repo (e.g. `/home/pi/enviropi`), create venv, install:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   # hardware deps usually already present from Pimoroni install
   ```

4. Copy and edit config:

   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```

   Set in `.env`:

   - `ENVIROPI_MOCK_SENSORS=false`
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`
   - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`, `OAUTH_ALLOWLIST`
   - `SESSION_SECRET` (long random string)
   - `ENVIROPI_DB=/home/pi/enviropi/data/enviropi.db`

5. In `config.yaml`, set `telegram_allowlist` to your Telegram numeric user id(s), and `dashboard_url` to the public HTTPS URL.

6. Install systemd units (adjust paths/user if needed):

   ```bash
   sudo cp systemd/enviropi-*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now enviropi-collector enviropi-web
   ```

## Google OAuth + HTTPS

Google requires a registered redirect URI. On a home Pi, use a tunnel (e.g. [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)) or reverse proxy with a domain.

1. Create a Google Cloud **OAuth 2.0 Client ID** (Web application).
2. Authorized redirect URI: `https://your-host/auth/callback` (must match `OAUTH_REDIRECT_URI`).
3. Put the client in **Testing** mode and add your Google account as a test user, or publish the app.
4. Set `OAUTH_ALLOWLIST=you@gmail.com` (comma-separated).
5. Bind the app to localhost (`WEB_HOST=127.0.0.1`) and put the tunnel/proxy in front.

## Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather); put the token in `.env`.
2. Message the bot, then find your chat id (e.g. via `@userinfobot` or getUpdates) → `TELEGRAM_ALERT_CHAT_ID`.
3. Add your user id to `telegram_allowlist` in `config.yaml`.

Commands (allowlisted users only):

| Command | Action |
|---------|--------|
| `/status` | Latest readings |
| `/alerts` | Effective thresholds |
| `/set <key> <value>` | Override threshold |
| `/reset <key>` | Clear override |
| `/mute [hours]` | Silence alerts |
| `/unmute` | Resume alerts |

Example keys: `temperature.high`, `humidity.low`, `gas_reducing.high`, `cooldown_sec`.

### Gas thresholds

MICS6814 reports **resistance (Ω)**, not ppm:

- **Reducing / NH3**: resistance **falls** as those gases rise → set `gas_*.high` as a **floor**; alert fires when reading goes **below** it.
- **Oxidising**: resistance **rises** with NO₂-like gases → alert when reading goes **above** `gas_oxidising.high`.

Leave gas thresholds `null` until you have a stable baseline (sensor needs warm-up; see `gas.baseline_warmup_min`). Optional `gas.relative_change_pct` alerts on % move vs a rolling baseline after warm-up.

## Alert defaults

| Condition | Default |
|-----------|---------|
| Temp high / low | 28°C / 10°C |
| Humidity high / low | 70% / 30% |
| Pressure, gases, noise, lux | off (`null`) |
| Cooldown | 30 minutes |

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
