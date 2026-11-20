"""FastAPI entry point: wires the routes, security middleware, static assets and health check."""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from coldline.agent import AgentService
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
    app.add_middleware(
        SecurityMiddleware,
        signing_key=settings.signing_key(),
        csp=content_security_policy(TEMPLATE_DIR / "base.html"),
        secure_cookies=settings.production,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Starlette's base class also covers routing errors (an unknown path is a Starlette 404, not FastAPI's).
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = "No page here." if exc.status_code == 404 and exc.detail == "Not Found" else str(exc.detail)
        return _error(request, exc.status_code, message)

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
