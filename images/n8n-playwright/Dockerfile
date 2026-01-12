# images/n8n-playwright/Dockerfile
FROM mcr.microsoft.com/playwright:v1.49.0-jammy

# 1) Installer n8n + le package Playwright (le package n'est pas inclus dans l'image Playwright)
RUN npm -g i n8n@2.2.4 playwright@1.49.0

# 2) Workaround: n8n cherche "community-packages.ee/..." mais certaines installs ont "community-packages/..."
#    On crée un lien symbolique si nécessaire (dans /usr/lib et /usr/local)
RUN set -eux; \
  for base in /usr/lib/node_modules /usr/local/lib/node_modules; do \
    if [ -d "$base/n8n/dist/modules/community-packages" ] && [ ! -e "$base/n8n/dist/modules/community-packages.ee" ]; then \
      ln -s "$base/n8n/dist/modules/community-packages" "$base/n8n/dist/modules/community-packages.ee"; \
    fi; \
  done

# 3) S'assurer qu'un utilisateur "node" existe (sans forcer l'UID)
RUN set -eux; \
  if ! id -u node >/dev/null 2>&1; then \
    useradd --create-home --shell /bin/bash node; \
  fi

# 4) Préparer les dossiers n8n + aligner le cache Playwright attendu sur Linux (~/.cache/ms-playwright)
#    Les images Playwright utilisent /ms-playwright => on symlink vers /ms-playwright
RUN set -eux; \
  mkdir -p /home/node/.cache /home/node/.n8n/nodes /root/.cache; \
  ln -sf /ms-playwright /home/node/.cache/ms-playwright; \
  ln -sf /ms-playwright /root/.cache/ms-playwright; \
  chown -R node:node /home/node

USER node
WORKDIR /home/node/.n8n/nodes

# 5) Installer le community node Playwright n8n (au build, pas au runtime)
RUN npm init -y \
 && npm install --omit=dev n8n-nodes-playwright

# 6) Variables utiles
ENV N8N_COMMUNITY_PACKAGES_ENABLED=true
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

EXPOSE 5678
CMD ["n8n"]
