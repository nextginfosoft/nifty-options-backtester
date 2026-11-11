# syntax=docker/dockerfile:1
# Build context is the repo root (see docker-compose.yml). Only paths allowed by
# .dockerignore are available.

# ---- front end ----------------------------------------------------------------
FROM node:24-slim AS web
WORKDIR /web
RUN npm install -g pnpm@11.11.0
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ---- API + static front end ---------------------------------------------------
FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY deploy/requirements-server.txt ./deploy/requirements-server.txt
RUN pip install -r deploy/requirements-server.txt

RUN useradd --system --uid 10001 --home /app app \
    && mkdir -p /app/data/.cache && chown -R app:app /app
COPY --chown=app:app . .
COPY --from=web --chown=app:app /web/dist /app/frontend/dist
RUN sed -i 's/\r$//' /app/deploy/entrypoint.sh && chmod +x /app/deploy/entrypoint.sh

USER app
ENV APP_ENV=production \
    STORAGE_DIR=/app/data/.cache \
    FRONTEND_DIST=/app/frontend/dist
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["uvicorn", "api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
