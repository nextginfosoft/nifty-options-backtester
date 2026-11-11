"""Application factory for the hosted dashboard.

Run locally:   uvicorn api.app:create_app --factory --port 8000
Production:    see deploy/ (Docker Compose + Caddy). Env vars are documented in
               deploy/.env.example.

Layout: everything under /api (public: /api/health and /api/auth/*; all else needs
the session cookie). If a built front end exists (frontend/dist or FRONTEND_DIST)
it is served at / with SPA fallback, so one process serves the whole app.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api import auth, autologin, broker_routes, credentials_routes, datarefresh, routes_ledger, server
from api.credentials import CredentialError, CredentialStore
from api.ledger import Ledger, LedgerError, NotFound
from api.settings import Settings, load_settings

logger = logging.getLogger(__name__)

_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def create_app(settings: Optional[Settings] = None, load_market: Optional[bool] = None,
               run_scheduler: Optional[bool] = None) -> FastAPI:
    settings = settings or load_settings()
    load_market = settings.load_market_on_start if load_market is None else load_market
    run_scheduler = load_market if run_scheduler is None else run_scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        restored = broker_routes.load_saved_token(settings.storage_dir)
        logger.info("Kite token restored from disk: %s", restored)
        try:
            credentials_routes.apply_to_environment(app.state.creds.load())
        except CredentialError as exc:
            logger.error("Stored Zerodha credentials unusable: %s", exc)
        task = asyncio.create_task(asyncio.to_thread(server.load_all)) if load_market else None
        sched = asyncio.create_task(autologin.scheduler(app)) if run_scheduler else None
        refresher = asyncio.create_task(datarefresh.scheduler()) if run_scheduler else None
        yield
        for t in (task, sched, refresher):
            if t and not t.done():
                t.cancel()

    app = FastAPI(
        title="Nifty Paper Trading API",
        version="2.0.0",
        docs_url="/api/docs" if settings.enable_docs else None,
        openapi_url="/api/openapi.json" if settings.enable_docs else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.ledger = Ledger(settings.storage_dir / "ledger.sqlite3")
    app.state.creds = CredentialStore(settings.storage_dir, settings.credentials_key, settings.is_production)

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(LedgerError)
    async def _bad_request(_: Request, exc: LedgerError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith(("/api/docs", "/api/openapi")):
            response.headers.setdefault("Content-Security-Policy", _CSP)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    api = APIRouter(prefix="/api")
    api.include_router(server.public_router)
    api.include_router(auth.router)
    protected = APIRouter(dependencies=[Depends(auth.require_auth)])
    protected.include_router(server.router)
    protected.include_router(routes_ledger.router)
    protected.include_router(broker_routes.router)
    protected.include_router(credentials_routes.router)
    api.include_router(protected)
    app.include_router(api)

    dist = settings.frontend_dist
    if dist is not None:
        dist = dist.resolve()
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/") or path == "api":
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and dist in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.app:create_app", factory=True, host="127.0.0.1", port=8000)
