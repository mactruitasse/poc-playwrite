# syntax=docker/dockerfile:1
FROM node:20.19.6-bookworm-slim

ARG N8N_VERSION=2.2.4
ARG PLAYWRIGHT_VERSION=1.49.0

ENV DEBIAN_FRONTEND=noninteractive \
    N8N_USER_FOLDER=/home/node/.n8n \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    N8N_LISTEN_ADDRESS=0.0.0.0 \
    # Ajout du PATH vers les binaires npm globaux
    PATH="/usr/local/bin:/usr/local/lib/node_modules/n8n/bin:${PATH}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl dumb-init \
 && rm -rf /var/lib/apt/lists/*

RUN npm -g install "n8n@${N8N_VERSION}" "playwright@${PLAYWRIGHT_VERSION}"

RUN npx --yes "playwright@${PLAYWRIGHT_VERSION}" install --with-deps

RUN set -eux; \
    mkdir -p /home/node/.cache /root/.cache /home/node/.n8n/nodes; \
    ln -sf /ms-playwright /home/node/.cache/ms-playwright; \
    ln -sf /ms-playwright /root/.cache/ms-playwright; \
    chmod -R a+rX /ms-playwright; \
    chown -R node:node /home/node

WORKDIR /home/node/.n8n/nodes
USER root
RUN npm init -y \
 && npm install --omit=dev n8n-nodes-playwright \
 && chown -R node:node /home/node/.n8n

RUN set -eux; \
    MOD_DIR="/usr/local/lib/node_modules/n8n/dist/modules"; \
    if [ -d "/usr/lib/node_modules/n8n/dist/modules" ]; then MOD_DIR="/usr/lib/node_modules/n8n/dist/modules"; fi; \
    if [ -d "${MOD_DIR}/community-packages" ] && [ ! -e "${MOD_DIR}/community-packages.ee" ]; then \
      ln -s community-packages "${MOD_DIR}/community-packages.ee"; \
    fi;

RUN set -eux; \
  for MODROOT in /usr/lib/node_modules/n8n/dist/modules /usr/local/lib/node_modules/n8n/dist/modules; do \
    [ -d "$MODROOT" ] || continue; \
    find "$MODROOT" -maxdepth 2 -type f -name "*.module.js" -print0 \
      | while IFS= read -r -d '' f; do \
          ln -sf "$(basename "$f")" "${f%.js}"; \
        done; \
  done

# Correction finale des permissions sur les binaires pour l'utilisateur node
RUN chmod +x /usr/local/bin/n8n

USER node
EXPOSE 5678
ENTRYPOINT ["dumb-init","--"]
CMD ["n8n"]
