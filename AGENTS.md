# EnviroPi agent notes

## Deploy to Pi (required reading)

Day-to-day code changes must use the **fast app deploy**. Do not stream the armv6 venv unless dependencies or Python changed.

### Fast path (default) — local wheel → artifacts only

```bash
./scripts/push-to-pi.sh          # or: make push
# equivalent: ./scripts/push-to-pi.sh --app
```

What it does:

1. Builds a pure-Python wheel on the Mac (`python -m build` → `dist/enviropi-*.whl`)
2. rsyncs **only** the wheel + systemd units + `config.example.yaml` / `.env.example` / `pyproject.toml`
3. On the Pi: `pip install --no-deps --force-reinstall` into the existing `.venv`, restart services

Prereq: a prior `--full` install so `/opt/embedded-stack/apps/enviropi/.venv` already exists.

Typical time: ~30–90s (vs ~10 min for a full venv push).

### Full path — when deps / Python / first install change

```bash
make build                       # Docker armv6 venv → dist/venv-armv6/
./scripts/push-to-pi.sh --full   # or: make push-full
```

Use `--full` for: new pip dependencies, hardware extras, Python minor bump, broken remote venv, or first bootstrap.

### Agent rules

- When the user says “push to pi” / “deploy” after app code changes → run `./scripts/push-to-pi.sh` (**--app**, default).
- Only run `--full` (or `make build` + `--full`) if deps changed, the remote `.venv` is missing, or the user asks for a full/venv deploy.
- Confirm Tailscale can resolve the host before deploying; if logged out, ask the user to log in rather than hanging on SSH.
- Do not commit `.env`, `config.yaml`, or `data/`.
- Prefer `git push` only when the user asks to publish to GitHub.

### Host identity

Resolved from `.env`: `ENVIROPI_HOST` or `ENVIROPI_SERVICE_USER@ENVIROPI_TAILSCALE_HOST`.
