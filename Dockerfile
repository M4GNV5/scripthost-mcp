FROM python:3.13-slim

# uv builds the per-project virtualenvs quickly; git/build tools cover most pip installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY scripthost ./scripthost
RUN uv pip install --system --no-cache .

# Scripts run as this unprivileged user, never as root
RUN useradd --create-home --uid 1000 scripthost && mkdir /data && chown scripthost /data
USER scripthost

ENV SCRIPTHOST_DATA_DIR=/data \
    UV_CACHE_DIR=/data/.uv-cache \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

ENTRYPOINT ["tini", "--"]
CMD ["scripthost-mcp"]
