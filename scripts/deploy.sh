#!/bin/bash
# scripts/deploy.sh

# 1. Build des images
docker build -t playwright-server-local:latest -f Dockerfile.worker .
docker build -t playwright-wrapper-local:latest ./playwright-wrapper

# 2. Chargement dans Kind
kind load docker-image playwright-server-local:latest --name pw
kind load docker-image playwright-wrapper-local:latest --name pw

# 3. Restart du déploiement
kubectl -n n8n-prod rollout restart deployment playwright-wrapper
