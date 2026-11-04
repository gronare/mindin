FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/gronare/mindin"
LABEL io.modelcontextprotocol.server.name="io.github.gronare/mindin"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY mindin ./mindin
ARG EXTRAS=""
RUN uv sync --frozen --no-dev $EXTRAS && ln -s mindin /app/.venv/bin/notes-vault-mcp

ENV VAULT_AUTH_DIR=/data/auth VAULT_CACHE_DIR=/data/cache
VOLUME /data

ENTRYPOINT ["mindin"]
