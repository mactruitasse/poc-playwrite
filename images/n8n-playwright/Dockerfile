FROM n8nio/n8n:2.2.5

USER node

# Installer le community node dans ~/.n8n/nodes
WORKDIR /home/node/.n8n/nodes

# S'assure que le dossier existe
RUN mkdir -p /home/node/.n8n/nodes

# Installer le node Playwright via pnpm (requis par le package)
RUN pnpm --version \
 && pnpm add n8n-nodes-playwright@0.2.21 \
 && pnpm rebuild n8n-nodes-playwright
