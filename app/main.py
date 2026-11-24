from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.web import router as web_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("chitoproject")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.uploads_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        settings.uploads_dir.chmod(0o700)
    except OSError:
        logger.warning("upload directory permissions could not be tightened")
    logger.info("application starting environment=%s", settings.app_env)
    yield
    await engine.dispose()
    logger.info("application stopped")


app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
if settings.is_production:
    host = urlparse(settings.public_base_url).hostname
    if host:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[host])

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(web_router)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'self'",
    )
    if settings.is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin/login", status_code=303)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "not_ready", "database": "unavailable"}, status_code=503)
    return JSONResponse({
        "status": "ready",
        "database": "connected",
        "connectors": {
            "telegram": bool(settings.telegram_bot_token),
            "bale": bool(settings.bale_bot_token),
        },
    })
