# syntax=docker/dockerfile:1.12@sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25
# Build frontend and base images are pinned by digest (tags kept for readers; update them deliberately).
# Dependencies come only from the committed uv.lock: --locked fails the build if it is out of date.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY --from=ghcr.io/astral-sh/uv:0.5.31@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
ENV COLDLINE_DATA_DIR=/data
EXPOSE 8080
CMD ["/app/.venv/bin/uvicorn", "coldline.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
