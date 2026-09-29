FROM python:3.13-slim-bookworm

ARG UV_VERSION=0.12.0
ARG PLAYWRIGHT_VERSION=1.62.0
ARG CHROME_FOR_TESTING_VERSION=150.0.7871.124
ARG CHROME_FOR_TESTING_SHA256=ccb11556d5946fcf15f09d175c34d8e4b4293a8ef2eb7c4efc28cb60ac4d12fd

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/social-crawler-venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CRAWLER_BROWSER_EXECUTABLE_PATH=/opt/chrome-for-testing/chrome-linux64/chrome \
    PATH="/opt/social-crawler-venv/bin:${PATH}"

WORKDIR /app

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md alembic.ini ./

RUN uv sync --locked --no-dev --no-install-project

RUN test "$(python -c 'from importlib.metadata import version; print(version("playwright"))')" = "${PLAYWRIGHT_VERSION}" \
    && playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# curl_cffi 0.16.x has an exact Chrome 150 preset while Playwright 1.62 bundles
# Chrome 151. Pin the official Chrome for Testing binary so browser, UA and TLS
# identities use one major version instead of silently drifting with dependencies.
RUN CHROME_VERSION="${CHROME_FOR_TESTING_VERSION}" \
    CHROME_SHA256="${CHROME_FOR_TESTING_SHA256}" \
    python - <<'PY'
import hashlib
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path

version = os.environ["CHROME_VERSION"]
expected = os.environ["CHROME_SHA256"]
url = f"https://storage.googleapis.com/chrome-for-testing-public/{version}/linux64/chrome-linux64.zip"
target = Path("/opt/chrome-for-testing")
target.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile() as archive:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=300) as response:
        while chunk := response.read(1024 * 1024):
            archive.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise SystemExit("Chrome for Testing archive checksum mismatch")
    archive.flush()
    archive.seek(0)
    with zipfile.ZipFile(archive) as source:
        source.extractall(target)
        for member in source.infolist():
            mode = (member.external_attr >> 16) & 0o777
            if mode:
                (target / member.filename).chmod(mode)
PY
RUN actual="$(${CRAWLER_BROWSER_EXECUTABLE_PATH} --version)" \
    && echo "${actual}" \
    && version="$(printf '%s\n' "${actual}" | awk '{print $NF}')" \
    && test "${version}" = "${CHROME_FOR_TESTING_VERSION}"

COPY deploy/docker/kasmvnc/install.sh /tmp/install-kasmvnc.sh
RUN sh /tmp/install-kasmvnc.sh && rm /tmp/install-kasmvnc.sh

RUN useradd --create-home --uid 10001 crawler \
    && mkdir -p /app/data /app/profiles /app/artifacts /home/crawler/.cache/social-crawler/locks /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R crawler:crawler /app/data /app/profiles /app/artifacts /home/crawler

COPY src ./src
COPY configs ./configs
COPY scripts/container_online_validation.py ./scripts/container_online_validation.py

RUN uv sync --locked --no-dev

USER crawler

EXPOSE 8765

CMD ["social-crawler-web", "--root", "/app"]
