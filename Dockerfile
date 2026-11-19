# Pin Hermes and use its upstream lockfile for runtime dependencies.
FROM ghcr.io/astral-sh/uv:0.12.9 AS uv
FROM ghcr.io/tailscale/tailscale:v1.102.5 AS tailscale
FROM python:3.14-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
RUN git clone --depth 1 --branch v0.22.7 https://github.com/NousResearch/hermes-agent.git /opt/hermes \
    && test "$(git -C /opt/hermes rev-parse HEAD)" = b200f5bc60e31588ba2cb60e288029c4a54ba5fa \
    && uv sync --project /opt/hermes --locked --no-dev --extra messaging --extra ddgs
WORKDIR /opt/bluebird
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY assistant assistant
COPY skill_tap skill_tap
COPY hermes hermes
RUN uv sync --locked --no-dev
RUN /opt/hermes/.venv/bin/python -m compileall -q /opt/hermes \
    && /opt/bluebird/.venv/bin/python -m compileall -q /opt/bluebird

FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 bluebird && useradd --create-home -u 10001 -g bluebird bluebird \
    && install -d -m 0700 -o bluebird -g bluebird /var/lib/bluebird
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscaled /usr/local/bin/
COPY --from=build /opt/hermes /opt/hermes
COPY --from=build /opt/bluebird /opt/bluebird
WORKDIR /opt/bluebird
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    A3_STATE_DIR=/var/lib/bluebird HERMES_HOME=/var/lib/bluebird/hermes \
    A3_PROJECT_ROOT=/opt/bluebird A3_HERMES_BIN=/opt/hermes/.venv/bin/hermes \
    A3_PUBLIC_HOST=0.0.0.0 A3_RUN_TAILSCALE=1 A3_SOCKS_PROXY=socks5h://127.0.0.1:1055 \
    A3_GOOGLE_TOKEN_FILE=/var/lib/bluebird/google-token.json PORT=8080
USER 10001:10001
EXPOSE 8080
CMD ["/opt/bluebird/.venv/bin/a3", "start"]
