from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from enviropi.config import (
    OVERRIDE_KEYS,
    effective_threshold_map,
    get_config,
    get_env,
    merge_overrides,
)
from enviropi.db import Database, to_iso, utc_now

logger = logging.getLogger("enviropi.web")

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
STATIC_DIR = PACKAGE_DIR / "static"

RANGE_HOURS = {
    "24h": (24, False),
    "7d": (24 * 7, True),
    "30d": (24 * 30, True),
    "1y": (24 * 365, True),
}


def create_app() -> FastAPI:
    env = get_env()
    config = get_config(env)
    db = Database(env.enviropi_db)

    app = FastAPI(title="EnviroPi", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=env.session_secret,
        session_cookie="enviropi_session",
        same_site="lax",
        https_only=env.oauth_redirect_uri.startswith("https://"),
        max_age=60 * 60 * 24 * 14,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    oauth = OAuth()
    oauth_ready = bool(env.google_client_id and env.google_client_secret)
    if oauth_ready:
        oauth.register(
            name="google",
            client_id=env.google_client_id,
            client_secret=env.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    app.state.env = env
    app.state.config = config
    app.state.db = db
    app.state.oauth = oauth
    app.state.oauth_ready = oauth_ready

    def current_user(request: Request) -> dict[str, Any] | None:
        sid = request.session.get("sid")
        if not sid:
            return None
        return db.get_session(sid)

    def require_user(request: Request) -> dict[str, Any]:
        user = current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Any:
        user = current_user(request)
        if user:
            return RedirectResponse("/", status_code=302)
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"oauth_configured": oauth_ready, "user": None},
        )

    @app.get("/auth/login")
    async def auth_login(request: Request) -> Any:
        if not oauth_ready:
            raise HTTPException(500, "Google OAuth is not configured")
        return await oauth.google.authorize_redirect(request, env.oauth_redirect_uri)

    @app.get("/auth/callback")
    async def auth_callback(request: Request) -> Any:
        if not oauth_ready:
            raise HTTPException(500, "Google OAuth is not configured")
        token = await oauth.google.authorize_access_token(request)
        info = token.get("userinfo") or {}
        email = (info.get("email") or "").lower()
        sub = info.get("sub")
        if not email or not sub:
            raise HTTPException(400, "Google account missing email")
        if email not in env.oauth_emails:
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {
                    "oauth_configured": True,
                    "error": f"{email} is not on the allowlist.",
                    "user": None,
                },
                status_code=403,
            )
        db.upsert_user(sub, email, info.get("name"))
        sid = secrets.token_urlsafe(32)
        db.create_session(sid, sub, email, utc_now() + timedelta(days=14))
        request.session["sid"] = sid
        return RedirectResponse("/", status_code=302)

    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> Any:
        sid = request.session.pop("sid", None)
        if sid:
            db.delete_session(sid)
        return RedirectResponse("/login", status_code=302)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, range: str = "24h") -> Any:
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if range not in RANGE_HOURS:
            range = "24h"
        latest = db.latest_sample()
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "user": user,
                "latest": latest,
                "range": range,
                "ranges": list(RANGE_HOURS.keys()),
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, user: dict = Depends(require_user)) -> Any:
        overrides = db.get_overrides()
        effective = effective_threshold_map(config, overrides)
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "user": user,
                "effective": effective,
                "overrides": overrides,
                "keys": [k for k in OVERRIDE_KEYS if k != "mute_until"],
                "saved": request.query_params.get("saved") == "1",
            },
        )

    @app.post("/settings")
    async def settings_save(
        request: Request,
        user: dict = Depends(require_user),
    ) -> Any:
        form = await request.form()
        for key in OVERRIDE_KEYS:
            if key == "mute_until":
                continue
            if key not in form:
                continue
            raw = str(form.get(key, "")).strip()
            if raw == "" or raw.lower() == "default":
                db.delete_override(key)
            else:
                db.set_override(key, raw, "dashboard")
        mute_hours = str(form.get("mute_hours", "")).strip()
        if mute_hours:
            try:
                hours = float(mute_hours)
                until = utc_now() + timedelta(hours=hours)
                db.set_override("mute_until", to_iso(until), "dashboard")
            except ValueError:
                pass
        if form.get("unmute") == "1":
            db.delete_override("mute_until")
        return RedirectResponse("/settings?saved=1", status_code=302)

    @app.get("/api/latest")
    async def api_latest(user: dict = Depends(require_user)) -> Any:
        sample = db.latest_sample()
        if not sample:
            return {"sample": None}
        return {"sample": sample}

    @app.get("/api/history")
    async def api_history(
        range: str = "24h",
        user: dict = Depends(require_user),
    ) -> Any:
        if range not in RANGE_HOURS:
            raise HTTPException(400, "Invalid range")
        hours, use_hourly = RANGE_HOURS[range]
        since = utc_now() - timedelta(hours=hours)
        points = db.history(since=since, use_hourly=use_hourly)
        return {"range": range, "hourly": use_hourly, "points": points}

    @app.get("/api/thresholds")
    async def api_thresholds(user: dict = Depends(require_user)) -> Any:
        overrides = db.get_overrides()
        return {
            "effective": effective_threshold_map(config, overrides),
            "overrides": overrides,
            "merged": merge_overrides(config, overrides).model_dump(),
        }

    @app.get("/healthz")
    async def healthz() -> Any:
        return {"ok": True}

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    env = get_env()
    uvicorn.run(
        "enviropi.web.app:create_app",
        factory=True,
        host=env.web_host,
        port=env.web_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
