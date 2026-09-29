ARG RUNTIME_IMAGE=social-crawler:latest

FROM node:24-bookworm-slim AS node-runtime

FROM ${RUNTIME_IMAGE}

ARG ADSPOWER_BROWSER_VERSION=2.1.1

ENV DEBIAN_FRONTEND=noninteractive \
    PORT=50325 \
    HOME=/home/crawler

USER root

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global "adspower-browser@${ADSPOWER_BROWSER_VERSION}" \
    && npm cache clean --force \
    && mkdir -p /home/crawler/.adspowerCli \
    && chown -R crawler:crawler \
        /home/crawler/.adspowerCli \
        /usr/local/lib/node_modules/adspower-browser

# The profile presents macOS: expose macOS font family names, hide Linux ones.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/docker/adspower/fonts/99-macos-families.conf /etc/fonts/conf.d/99-macos-families.conf
RUN fc-cache -f

COPY --chown=crawler:crawler deploy/docker/adspower/entrypoint.sh /usr/local/bin/adspower-entrypoint
COPY --chown=crawler:crawler deploy/docker/adspower/healthcheck.sh /usr/local/bin/adspower-healthy

RUN chmod 0755 /usr/local/bin/adspower-entrypoint /usr/local/bin/adspower-healthy

USER crawler

EXPOSE 50325

ENTRYPOINT ["/usr/local/bin/adspower-entrypoint"]
