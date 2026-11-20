"""FastAPI entry point: wires the web routes, static assets and health check."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from coldline.contracts import DISCLAIMER

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="ColdLine", version="0.1.0")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return f"<!doctype html><title>ColdLine</title><p>{DISCLAIMER}</p><h1>ColdLine</h1><p>Deploying soon.</p>"
