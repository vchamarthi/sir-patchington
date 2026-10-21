# Pinned official images; this service image targets Linux amd64.
FROM --platform=linux/amd64 ghcr.io/astral-sh/uv@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 AS uv
FROM --platform=linux/amd64 node@sha256:3ed98443660ff48413b5af19ecefce3b9d50845a901a43420d47cc63a9fddbe9 AS node
FROM --platform=linux/amd64 python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy HOME=/home/patchington PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONPATH=/app/src
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN apt-get update && apt-get install --no-install-recommends -y \
    ca-certificates git docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
RUN /usr/sbin/groupadd --gid 10001 patchington \
    && /usr/sbin/useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin patchington
WORKDIR /app
COPY deploy/claude/package.json deploy/claude/package-lock.json /opt/claude/
RUN node --jitless -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)' \
    && NODE_OPTIONS=--jitless npm ci --prefix /opt/claude --ignore-scripts --omit=dev --no-audit --no-fund \
    && ln -s /opt/claude/node_modules/.bin/claude /usr/local/bin/claude \
    && NODE_OPTIONS=--jitless npm cache clean --force
# The pinned, reviewed postinstall only links the integrity-checked native package.
RUN node --jitless /opt/claude/node_modules/@anthropic-ai/claude-code/install.cjs \
    && claude --version
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src /app/src
COPY deploy/entrypoint.py /app/deploy/entrypoint.py
RUN mkdir -p /var/lib/patchington/artifacts /home/patchington/.claude \
    /home/patchington/.cache/sir-patchington/checks \
    && chown -R 10001:10001 /var/lib/patchington /home/patchington
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/.venv/bin/python", "/app/deploy/entrypoint.py"]
CMD ["--help"]
