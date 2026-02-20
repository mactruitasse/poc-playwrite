#!/usr/bin/env bash
set -euo pipefail

############################################
# Variables configurables
############################################
NS="${NS:-default}"
WRAPPER_NS="${WRAPPER_NS:-default}"
WRAPPER_SVC="${WRAPPER_SVC:-playwright-wrapper}"
WRAPPER_PORT="${WRAPPER_PORT:-8080}"
SECRET_NAME="${SECRET_NAME:-playwright-wrapper-secret}"

CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.6.0}"
POD_NAME="${POD_NAME:-curltest-$(date +%Y%m%d-%H%M%S)}"

WORKDIR="${WORKDIR:-/tmp/pw-incluster-test}"
mkdir -p "$WORKDIR"
OUT="${WORKDIR}/out-${POD_NAME}.log"

############################################
# Helpers
############################################
die() { echo "ERREUR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "commande requise introuvable: $1"; }

need kubectl
need base64
need date
need tee

############################################
# Récupération token
############################################
TOKEN="$(kubectl -n "$WRAPPER_NS" get secret "$SECRET_NAME" -o jsonpath='{.data.API_TOKEN}' | base64 -d)"
[[ -n "${TOKEN:-}" ]] || die "API_TOKEN vide (secret=${WRAPPER_NS}/${SECRET_NAME})"

BASE="http://${WRAPPER_SVC}.${WRAPPER_NS}.svc:${WRAPPER_PORT}"

############################################
# Création pod curl
############################################
echo "==> Création pod ${NS}/${POD_NAME}"
kubectl -n "$NS" run "$POD_NAME" --restart=Never --image="$CURL_IMAGE" --command -- sleep 3600 >/dev/null

cleanup() { kubectl -n "$NS" delete pod "$POD_NAME" --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> Attente pod Ready"
kubectl -n "$NS" wait --for=condition=Ready "pod/${POD_NAME}" --timeout=60s >/dev/null
echo "✅ Pod prêt"

############################################
# Tests
############################################
{
  echo "### BASE=${BASE}"
  echo

  echo "### GET /health"
  kubectl -n "$NS" exec "$POD_NAME" -- sh -lc "curl -sS -i '${BASE}/health'"
  echo

  echo "### POST /sessions"
  kubectl -n "$NS" exec "$POD_NAME" -- sh -lc \
    "curl -sS -i -H 'X-API-Token: ${TOKEN}' -H 'Content-Type: application/json' -X POST '${BASE}/sessions' -d '{}'"
  echo

  echo "### GET /mcp (info)"
  kubectl -n "$NS" exec "$POD_NAME" -- sh -lc \
    "curl -sS -i -H 'X-API-Token: ${TOKEN}' '${BASE}/mcp'"
  echo

  echo "### POST /mcp (info)"
  kubectl -n "$NS" exec "$POD_NAME" -- sh -lc \
    "curl -sS -i -X POST -H 'X-API-Token: ${TOKEN}' '${BASE}/mcp'"
  echo
} | tee "$OUT"

############################################
# Vérification
############################################
echo
echo "==> Vérification attendue"
echo "- /health : HTTP 200"
echo "- /sessions : HTTP 200/201 (et JSON)"
echo "- /mcp : aujourd'hui tu auras probablement 404 (tant que la route n'est pas ajoutée)"
echo
echo "Logs: $OUT"
