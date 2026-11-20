FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock* README.md ./
RUN uv sync --no-dev --no-install-project
COPY src ./src
RUN uv sync --no-dev
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
ENV COLDLINE_DATA_DIR=/data
EXPOSE 8080
CMD ["/app/.venv/bin/uvicorn", "coldline.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
