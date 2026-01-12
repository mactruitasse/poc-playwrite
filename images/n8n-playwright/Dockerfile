FROM node:20-bullseye

# 1) Outils
RUN npm i -g n8n@2.2.5
RUN npm i -g pnpm@9 --force

# 2) Dossier des community nodes
RUN mkdir -p /home/node/.n8n/nodes \
 && mkdir -p /home/node/.cache/ms-playwright \
 && chown -R node:node /home/node/.n8n /home/node/.cache

# Playwright install location (aligné avec le cache Linux par défaut ~/.cache/ms-playwright) :contentReference[oaicite:2]{index=2}
ENV PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright
ENV PATH="/home/node/.n8n/nodes/node_modules/.bin:${PATH}"
WORKDIR /home/node/.n8n/nodes

# 3) Installer le package Playwright + le community node
#    + installer Chromium + dépendances système (install --with-deps) :contentReference[oaicite:3]{index=3}
RUN pnpm add playwright@1.49.0 n8n-nodes-playwright@0.2.21 \
 && pnpm exec playwright install --with-deps chromium \
 && pnpm rebuild n8n-nodes-playwright

# 4) Runtime
USER node
ENV HOME=/home/node
EXPOSE 5678
CMD ["n8n"]
