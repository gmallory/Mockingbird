FROM python:3.14-slim-bookworm

# uv provides the project toolchain (deps + runner), matching local dev.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# ffmpeg decodes non-WAV clone clips (browser MediaRecorder sends webm/opus)
# for the self-hosted instant-clone path (M5b).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Locked dependencies first so the layer caches across source-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# HTTP clone/health on 8001; its lifespan also starts the gRPC audio server on 50051.
EXPOSE 8001 50051
CMD ["uv", "run", "--no-sync", "uvicorn", "app.health:app", "--host", "0.0.0.0", "--port", "8001"]
