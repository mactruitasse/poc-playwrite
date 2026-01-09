#!/usr/bin/env bash
set -euo pipefail

# This script downloads (vendors) the community-charts n8n Helm chart into ./charts/n8n
# so Argo CD can render it from your Git repository.
#
# Requirements: helm v3

CHART_VERSION="${CHART_VERSION:-}"  # optional: set CHART_VERSION=1.16.18

REPO_NAME="community-charts"
REPO_URL="https://community-charts.github.io/helm-charts"
CHART_NAME="n8n"
DEST_DIR="charts"

if ! command -v helm >/dev/null 2>&1; then
  echo "ERROR: helm not found in PATH" >&2
  exit 1
fi

mkdir -p "${DEST_DIR}"

helm repo add "${REPO_NAME}" "${REPO_URL}" >/dev/null
helm repo update >/dev/null

# Remove any previous vendored chart
rm -rf "${DEST_DIR}/${CHART_NAME}"

if [[ -n "${CHART_VERSION}" ]]; then
  helm pull "${REPO_NAME}/${CHART_NAME}" --version "${CHART_VERSION}" --untar --untardir "${DEST_DIR}"
else
  helm pull "${REPO_NAME}/${CHART_NAME}" --untar --untardir "${DEST_DIR}"
fi

echo "Vendored chart at: ${DEST_DIR}/${CHART_NAME}"
echo "Next: git add charts/n8n && git commit -m 'Vendor n8n chart' && git push"
