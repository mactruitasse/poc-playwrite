# syntax=docker/dockerfile:1

FROM node:20.19.6-bookworm-slim

ARG N8N_VERSION=2.2.4
ARG PLAYWRIGHT_VERSION=1.49.0

ENV DEBIAN_FRONTEND=noninteractive \
    N8N_USER_FOLDER=/home/node/.n8n \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Outils minimaux + init propre
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl dumb-init \
 && rm -rf /var/lib/apt/lists/*

# Installer n8n + Playwright (package)
RUN npm -g install "n8n@${N8N_VERSION}" "playwright@${PLAYWRIGHT_VERSION}"

# Installer navigateurs + dépendances OS (Playwright doc)
# PLAYWRIGHT_BROWSERS_PATH force l'emplacement d'installation des navigateurs
RUN npx --yes "playwright@${PLAYWRIGHT_VERSION}" install --with-deps

# Préparer caches pour satisfaire à la fois root et l'utilisateur node
RUN set -eux; \
    mkdir -p /home/node/.cache /root/.cache /home/node/.n8n/nodes; \
    ln -sf /ms-playwright /home/node/.cache/ms-playwright; \
    ln -sf /ms-playwright /root/.cache/ms-playwright; \
    chmod -R a+rX /ms-playwright; \
    chown -R node:node /home/node

# Installer le community node (le postinstall peut faire des checks/installs)
WORKDIR /home/node/.n8n/nodes
USER root
RUN npm init -y \
 && npm install --omit=dev n8n-nodes-playwright \
 && chown -R node:node /home/node/.n8n

# Workaround ESM: n8n tente d'importer ".../*.module" (sans .js). En ESM, Node ne cherche pas les extensions.
# On crée donc des liens "xxx.module" -> "xxx.module.js" dans tous les modules.

# Patch "community-packages.ee" si n8n attend le suffixe .ee
# (on vise /usr/local/lib/node_modules ou /usr/lib/node_modules selon l'image)
RUN set -eux; \
    MOD_DIR="/usr/local/lib/node_modules/n8n/dist/modules"; \
    if [ -d "/usr/lib/node_modules/n8n/dist/modules" ]; then MOD_DIR="/usr/lib/node_modules/n8n/dist/modules"; fi; \
    if [ -d "${MOD_DIR}/community-packages" ] && [ ! -e "${MOD_DIR}/community-packages.ee" ]; then \
      ln -s community-packages "${MOD_DIR}/community-packages.ee"; \
    fi; \
    # Debug optionnel (ne casse pas le build si ça échoue)
    node -e "require('${MOD_DIR}/community-packages.ee/community-packages.module')" || true

# Workaround ESM: n8n tente d'importer ".../*.module" (sans .js). En ESM, Node ne cherche pas les extensions.
# On crée donc des liens "xxx.module" -> "xxx.module.js" dans tous les modules.
RUN set -eux; \
  for MODROOT in /usr/lib/node_modules/n8n/dist/modules /usr/local/lib/node_modules/n8n/dist/modules; do \
    [ -d "$MODROOT" ] || continue; \
    find "$MODROOT" -maxdepth 2 -type f -name "*.module.js" -print0 \
      | while IFS= read -r -d '' f; do \
          ln -sf "$(basename "$f")" "${f%.js}"; \
        done; \
  done


USER node
EXPOSE 5678
ENTRYPOINT ["dumb-init","--"]
CMD ["n8n"]
