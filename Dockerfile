# syntax=docker/dockerfile:1.7
# AgentOS API / worker image.
FROM python:3.12-slim-bookworm AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.12.6
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime
# The docker CLI is only used by the worker's sandbox runner (AGENTOS_SANDBOX_BACKEND=docker).
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker
RUN groupadd --system agentos && useradd --system --gid agentos --home /app agentos
WORKDIR /app
COPY --from=build /app /app
COPY fixtures ./fixtures
COPY examples ./examples
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER agentos
EXPOSE 8000 9101
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health/live')" || exit 1
CMD ["python", "-m", "app.cli", "api"]
