FROM mcr.microsoft.com/playwright:v1.49.0-jammy

# n8n + pnpm
RUN npm i -g n8n@2.2.5
RUN npm i -g pnpm@9 --force

# Vérifier que l'image contient bien les navigateurs (répertoire attendu dans ces images)
RUN test -d /ms-playwright

# IMPORTANT : rendre les navigateurs visibles au chemin par défaut (~/.cache/ms-playwright)
# Le postinstall de n8n-nodes-playwright cherche /root/.cache/ms-playwright (home=root pendant le build)
RUN mkdir -p /root/.cache \
 && ln -s /ms-playwright /root/.cache/ms-playwright || true

# Et aussi pour l'utilisateur runtime (souvent non-root) : /home/node/.cache/ms-playwright
RUN mkdir -p /home/node/.cache \
 && ln -s /ms-playwright /home/node/.cache/ms-playwright || true

# Dossier community nodes n8n
RUN mkdir -p /home/node/.n8n/nodes
ENV PATH="/home/node/.n8n/nodes/node_modules/.bin:${PATH}"
WORKDIR /home/node/.n8n/nodes

# Installe le node + playwright (le postinstall devrait maintenant COPIER depuis /root/.cache/ms-playwright)
RUN pnpm add playwright@1.49.0 n8n-nodes-playwright@0.2.21

EXPOSE 5678

# Aligner avec les charts k8s qui tournent souvent en uid 1000
USER 1000
ENV HOME=/home/node

CMD ["n8n"]
