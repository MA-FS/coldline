"""FastAPI entry point: wires the routes, security middleware, static assets and health check."""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from coldline.agent import AgentService
from coldline.web import preview
from coldline.web.render import STATIC_DIR, TEMPLATE_DIR, base_context, templates
from coldline.web.routes import router as pages_router
from coldline.web.routes_actions import router as actions_router
from coldline.web.runtime import Runtime
from coldline.web.security import SecurityMiddleware, content_security_policy
from coldline.web.settings import WebSettings


def create_app(settings: WebSettings | None = None, agent: AgentService | None = None) -> FastAPI:
    settings = settings or WebSettings.from_env()
    app = FastAPI(
        title="ColdLine",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = Runtime.create(settings, agent)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(pages_router)
    app.include_router(actions_router)
    app.include_router(preview.styleguide_router)
    if settings.enable_previews:
        app.include_router(preview.router)
    app.add_middleware(
        SecurityMiddleware,
        signing_key=settings.signing_key(),
        csp=content_security_policy(TEMPLATE_DIR / "base.html"),
        secure_cookies=settings.production,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        return _error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        return _error(request, 422, "The request was missing a field or had an invalid value.")

    return app


def _error(request: Request, status: int, message: str) -> Response:
    """htmx requests get a toast; page loads get a page; everything else gets JSON."""
    if request.headers.get("hx-request"):
        trigger = {"toast": {"title": f"Request refused ({status})", "text": message, "tone": "red"}}
        return JSONResponse({"detail": message}, status, headers={"HX-Trigger": json.dumps(trigger)})
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        ctx = {**base_context("", None, title=str(status)), "status": status, "message": message}
        return templates.TemplateResponse(request, "pages/error.html", ctx, status_code=status)
    return JSONResponse({"detail": message}, status)


app = create_app()

__all__ = ["HTMLResponse", "app", "create_app"]
