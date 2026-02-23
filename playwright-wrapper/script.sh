#!/usr/bin/env bash
set -euo pipefail

############################################
# CONFIG (override via env vars)
############################################
NAMESPACE="${NAMESPACE:-n8n-prod}"
DEPLOYMENT="${DEPLOYMENT:-playwright-wrapper}"
CONTAINER_NAME="${CONTAINER_NAME:-wrapper}"

KIND_CLUSTER="${KIND_CLUSTER:-pw}"

IMAGE_REPO="${IMAGE_REPO:-playwright-wrapper}"
IMAGE_TAG="${IMAGE_TAG:-local-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"

DOCKERFILE="${DOCKERFILE:-Dockerfile}"
BUILD_CONTEXT="${BUILD_CONTEXT:-.}"

ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-240s}"

CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.6.0}"

# Timeouts
CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-3}"
CURL_MAX_TIME_HEALTH="${CURL_MAX_TIME_HEALTH:-10}"
CURL_MAX_TIME_SSE_HEADERS="${CURL_MAX_TIME_SSE_HEADERS:-10}"
CURL_MAX_TIME_MCP="${CURL_MAX_TIME_MCP:-10}"

# Retries health (post-rollout can be racy)
HEALTH_RETRIES="${HEALTH_RETRIES:-15}"
HEALTH_SLEEP_SECONDS="${HEALTH_SLEEP_SECONDS:-1}"

# Service target (force FQDN to avoid search/short-name quirks)
SVC_PORT="${SVC_PORT:-8080}"
SVC_FQDN="${SVC_FQDN:-${DEPLOYMENT}.${NAMESPACE}.svc.cluster.local}"

############################################
# PRECHECKS
############################################
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing dependency: $1" >&2; exit 1; }; }
need docker
need kind
need kubectl
need grep
need tail
need cut
need sed

dump_debug() {
  echo
  echo "[!] Debug dump (ns=${NAMESPACE}, deploy=${DEPLOYMENT})" >&2
  kubectl -n "${NAMESPACE}" get svc "${DEPLOYMENT}" -o wide >&2 || true
  kubectl -n "${NAMESPACE}" get endpoints "${DEPLOYMENT}" -o wide >&2 || true
  kubectl -n "${NAMESPACE}" get endpointslice -l kubernetes.io/service-name="${DEPLOYMENT}" -o wide >&2 || true
  kubectl -n "${NAMESPACE}" describe deploy "${DEPLOYMENT}" >&2 || true
  local pod=""
  pod="$(kubectl -n "${NAMESPACE}" get pods --sort-by=.metadata.creationTimestamp -o name \
    | grep -E "^pod/${DEPLOYMENT}-" | tail -n 1 | cut -d/ -f2 || true)"
  if [[ -n "${pod}" ]]; then
    echo "[!] Last pod: ${pod}" >&2
    kubectl -n "${NAMESPACE}" logs "${pod}" --tail=200 >&2 || true
    kubectl -n "${NAMESPACE}" describe pod "${pod}" | sed -n '/Events:/,$p' >&2 || true
  else
    kubectl -n "${NAMESPACE}" get pods -o wide >&2 || true
  fi
}

############################################
# BUILD
############################################
echo "[+] Build image: ${IMAGE}"
docker build -f "${DOCKERFILE}" -t "${IMAGE}" "${BUILD_CONTEXT}"

############################################
# KIND LOAD
############################################
echo "[+] kind load docker-image \"${IMAGE}\" --name \"${KIND_CLUSTER}\""
kind load docker-image "${IMAGE}" --name "${KIND_CLUSTER}"

############################################
# DEPLOY UPDATE + ROLLOUT
############################################
echo "[+] kubectl set image deploy/${DEPLOYMENT} ${CONTAINER_NAME}=${IMAGE} -n ${NAMESPACE}"
kubectl -n "${NAMESPACE}" set image "deploy/${DEPLOYMENT}" "${CONTAINER_NAME}=${IMAGE}"

echo "[+] rollout restart deploy/${DEPLOYMENT} -n ${NAMESPACE}"
kubectl -n "${NAMESPACE}" rollout restart "deploy/${DEPLOYMENT}"

echo "[+] rollout status deploy/${DEPLOYMENT} (timeout=${ROLLOUT_TIMEOUT})"
kubectl -n "${NAMESPACE}" rollout status "deploy/${DEPLOYMENT}" --timeout="${ROLLOUT_TIMEOUT}"

############################################
# TESTS
############################################
echo
echo "[+] Verify svc/endpoints (quick)"
kubectl -n "${NAMESPACE}" get svc "${DEPLOYMENT}" -o wide
kubectl -n "${NAMESPACE}" get endpoints "${DEPLOYMENT}" -o wide || true
kubectl -n "${NAMESPACE}" get endpointslice -l kubernetes.io/service-name="${DEPLOYMENT}" -o wide || true

echo
echo "[+] Test /health via FQDN=${SVC_FQDN} (retries=${HEALTH_RETRIES}, max-time ${CURL_MAX_TIME_HEALTH})"
attempt=1
while true; do
  set +e
  kubectl -n "${NAMESPACE}" run "curltest-health-${attempt}" --rm -i --restart=Never --image="${CURL_IMAGE}" -- \
    sh -lc "curl -sS -i --connect-timeout ${CURL_CONNECT_TIMEOUT} --max-time ${CURL_MAX_TIME_HEALTH} http://${SVC_FQDN}:${SVC_PORT}/health"
  rc=$?
  set -e

  if [[ $rc -eq 0 ]]; then
    echo "[+] OK: health responded"
    break
  fi

  if [[ $attempt -ge $HEALTH_RETRIES ]]; then
    echo "[!] Health check failed after ${HEALTH_RETRIES} attempts" >&2
    dump_debug
    exit 1
  fi

  echo "[+] Health attempt ${attempt} failed (rc=${rc}); sleep ${HEALTH_SLEEP_SECONDS}s"
  attempt=$((attempt+1))
  sleep "${HEALTH_SLEEP_SECONDS}"
done

echo
echo "[+] Test /sse headers (no infinite read; validate content-type)"
kubectl -n "${NAMESPACE}" run curltest-sse --rm -i --restart=Never --image="${CURL_IMAGE}" -- \
  sh -lc "set -e; \
    out=\$(curl -sS -i --connect-timeout ${CURL_CONNECT_TIMEOUT} --max-time ${CURL_MAX_TIME_SSE_HEADERS} \
      -H 'Accept: text/event-stream' \
      http://${SVC_FQDN}:${SVC_PORT}/sse \
      2>/dev/null | head -n 30); \
    echo \"\$out\" | sed -n '1,20p'; \
    echo \"\$out\" | grep -qi 'content-type: text/event-stream'"

echo
echo "[+] Test /mcp (max-time ${CURL_MAX_TIME_MCP})"
set +e
kubectl -n "${NAMESPACE}" run curltest-mcp --rm -i --restart=Never --image="${CURL_IMAGE}" -- \
  sh -lc "curl -sS -i --connect-timeout ${CURL_CONNECT_TIMEOUT} --max-time ${CURL_MAX_TIME_MCP} \
    -H 'Accept: application/json, text/event-stream' \
    http://${SVC_FQDN}:${SVC_PORT}/mcp | head -n 120"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  echo "[!] MCP test failed (rc=${rc})" >&2
  dump_debug
  exit 1
fi

############################################
# LOGS
############################################
echo
echo "[+] Wrapper logs (tail 250)"
POD="$(kubectl -n "${NAMESPACE}" get pods --sort-by=.metadata.creationTimestamp -o name \
  | grep -E "^pod/${DEPLOYMENT}-" | tail -n 1 | cut -d/ -f2 || true)"

if [[ -z "${POD}" ]]; then
  echo "ERROR: could not find a pod matching prefix '${DEPLOYMENT}-' in ns ${NAMESPACE}" >&2
  kubectl -n "${NAMESPACE}" get pods -o wide >&2 || true
  exit 1
fi

echo "[+] Pod: ${POD}"
kubectl -n "${NAMESPACE}" logs "${POD}" --tail=250

echo
echo "[OK] Deployed image: ${IMAGE}"
