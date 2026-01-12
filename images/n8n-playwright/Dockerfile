FROM mcr.microsoft.com/playwright:v1.49.0-jammy

RUN npm -g i n8n@2.2.4 playwright@1.49.0

# user "node" sans UID forcé (UID 1000 est déjà utilisé dans l'image)
RUN set -eux; \
    if ! id -u node >/dev/null 2>&1; then \
      useradd --create-home --shell /bin/bash node; \
    fi

RUN set -eux; \
    mkdir -p /home/node/.cache /home/node/.n8n/nodes /root/.cache; \
    ln -sf /ms-playwright /home/node/.cache/ms-playwright; \
    ln -sf /ms-playwright /root/.cache/ms-playwright; \
    chown -R node:node /home/node

USER node
WORKDIR /home/node/.n8n/nodes

RUN npm init -y \
 && npm install --omit=dev n8n-nodes-playwright

ENV N8N_COMMUNITY_PACKAGES_ENABLED=true
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

EXPOSE 5678
CMD ["n8n"]
