FROM node:22-alpine AS web
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/client-sdk/package.json packages/client-sdk/package.json
RUN npm ci
COPY apps/web apps/web
COPY packages/client-sdk packages/client-sdk
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY services services
RUN uv sync --frozen --no-dev --extra voice --extra vosk
COPY config config
COPY migrations migrations
COPY alembic.ini ./
COPY --from=web /app/apps/web/dist apps/web/dist
RUN useradd --create-home --uid 10001 voice && mkdir .data && chown voice:voice .data
USER voice
ENV PATH="/app/.venv/bin:$PATH" VOICE_HOST=0.0.0.0
EXPOSE 8000
CMD ["voice-agent", "serve"]
