#!/bin/bash
# scripts/kind-setup-images.sh

CLUSTER_NAME="pw"

echo "📥 Tagging and loading images into Kind cluster: $CLUSTER_NAME..."

# 1. Nginx Ingress Controller (v1.10.0 est celle que tu as)
docker tag ffcc66479b5b my-local-nginx:v1
kind load docker-image my-local-nginx:v1 --name $CLUSTER_NAME

# 2. ArgoCD Dex (L'init container qui bloquait)
docker tag quay.io/argoproj/argocd:v3.3.2 my-argocd:v1
kind load docker-image my-argocd:v1 --name $CLUSTER_NAME

# 3. n8n Custom Image
kind load docker-image n8n-playwright-local:2.2.4-pw1.49.0-fix5 --name $CLUSTER_NAME

echo "✅ All images loaded."
