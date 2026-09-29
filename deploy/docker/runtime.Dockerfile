# Reuse an immutable deployed runtime when dependencies have not changed.
# Supply RUNTIME_IMAGE as a registry image pinned with @sha256:... .
ARG RUNTIME_IMAGE=social-crawler:latest
FROM ${RUNTIME_IMAGE}

USER root
WORKDIR /app

COPY deploy/docker/kasmvnc/install.sh /tmp/install-kasmvnc.sh
RUN sh /tmp/install-kasmvnc.sh && rm /tmp/install-kasmvnc.sh

# Fail instead of silently deploying code against a different dependency set.
# Tool-only TOML sections (for example Ruff excludes) do not affect the runtime.
COPY pyproject.toml uv.lock /tmp/crawler-runtime-check/
RUN python - <<'PY'
import tomllib
from pathlib import Path

deployed = tomllib.loads(Path("/app/pyproject.toml").read_text())
candidate = tomllib.loads(Path("/tmp/crawler-runtime-check/pyproject.toml").read_text())
for section in ("build-system", "project"):
    if deployed.get(section) != candidate.get(section):
        raise SystemExit(f"runtime pyproject section changed: {section}")
PY
RUN cmp /app/uv.lock /tmp/crawler-runtime-check/uv.lock \
    && rm -rf /tmp/crawler-runtime-check /app/src /app/configs

COPY src ./src
COPY configs ./configs
COPY scripts/container_online_validation.py ./scripts/container_online_validation.py
RUN python -m compileall -q /app/src
RUN python - <<'PY'
from social_crawler.environments.browser_runtime import browser_runtime_identity

identity = browser_runtime_identity()
if identity.get("aligned") is not True:
    raise SystemExit(f"runtime Chrome identity is not aligned: {identity}")
PY

USER crawler
