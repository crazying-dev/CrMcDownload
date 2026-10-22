FROM ghcr.io/astral-sh/uv:0.12 AS uv

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    MIRROR_LISTEN=0.0.0.0:8081 \
    MIRROR_CACHE_DIR=/data

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock main.py ./
COPY server_mirror ./server_mirror

RUN uv sync --frozen --no-dev

EXPOSE 8081
VOLUME ["/data"]

CMD ["uv", "run", "--frozen", "main.py"]
