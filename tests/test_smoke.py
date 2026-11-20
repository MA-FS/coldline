from pathlib import Path

from fastapi.testclient import TestClient

from coldline.main import app


def test_healthz() -> None:
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}


def test_build_inputs_are_pinned_and_the_image_builds_nothing() -> None:
    """The lock pins every runtime dependency; the build backend is the one input it cannot. It is pinned
    exactly, and the image never needs it: it installs only the lock and runs the app from source."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    build = tomllib.loads((root / "pyproject.toml").read_text())["build-system"]["requires"]
    assert build and all("==" in req for req in build), build
    syncs = [line for line in (root / "Dockerfile").read_text().splitlines() if "uv sync" in line]
    assert syncs and all("--locked" in s and "--no-install-project" in s for s in syncs), syncs
    assert "ENV PYTHONPATH=/app/src" in (root / "Dockerfile").read_text()
