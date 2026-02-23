#!/usr/bin/env bash
set -euo pipefail

############################################
# CONFIG
############################################
NS="${NS:-n8n-prod}"

WRAPPER_DEPLOY="${WRAPPER_DEPLOY:-playwright-wrapper}"
WRAPPER_SVC="${WRAPPER_SVC:-playwright-wrapper}"
WRAPPER_PORT="${WRAPPER_PORT:-8080}"
WRAPPER_CONTAINER="${WRAPPER_CONTAINER:-wrapper}"

SECRET_NAME="${SECRET_NAME:-playwright-wrapper-secret}"
SECRET_KEY="${SECRET_KEY:-API_TOKEN}"

CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.6.0}"

CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-3}"
MAX_TIME_HEALTH="${MAX_TIME_HEALTH:-8}"
MAX_TIME_SESSIONS="${MAX_TIME_SESSIONS:-15}"

HEALTH_RETRIES="${HEALTH_RETRIES:-20}"
HEALTH_SLEEP="${HEALTH_SLEEP:-1}"

############################################
# PRECHECKS
############################################
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing dependency: $1" >&2; exit 1; }; }
need kubectl
need sed
need grep
need head
need tail
need date
need base64
need awk

BK="./triage_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BK"

echo "[+] Context: $(kubectl config current-context 2>/dev/null || true)" | tee "$BK/context.txt"
echo "[+] Namespace: $NS" | tee "$BK/ns.txt"
echo

############################################
# 1) WRAPPER SERVICE + ENDPOINTS
############################################
echo "[+] Wrapper svc/endpointslice"
kubectl -n "$NS" get svc "$WRAPPER_SVC" -o wide | tee "$BK/wrapper_svc.txt" || true
kubectl -n "$NS" get endpointslice -l "kubernetes.io/service-name=${WRAPPER_SVC}" -o yaml > "$BK/wrapper_endpointslice.yaml" || true
echo "Saved: $BK/wrapper_endpointslice.yaml"
echo

############################################
# 2) WRAPPER POD + DIRECT /health
############################################
WRAP_POD="$(kubectl -n "$NS" get pod -l "app.kubernetes.io/name=${WRAPPER_DEPLOYMENT:-playwright-wrapper},app.kubernetes.io/instance=${INSTANCE_LABEL:-n8n-prod}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "${WRAP_POD}" ]]; then
  # fallback: take newest pod matching deploy prefix
  WRAP_POD="$(kubectl -n "$NS" get pods --sort-by=.metadata.creationTimestamp -o name \
    | grep -E "^pod/${WRAPPER_DEPLOY}-" | tail -n 1 | cut -d/ -f2 || true)"
fi

echo "[+] Wrapper pod: ${WRAP_POD:-<not found>}" | tee "$BK/wrapper_pod.txt"
if [[ -n "$WRAP_POD" ]]; then
  kubectl -n "$NS" get pod "$WRAP_POD" -o wide | tee "$BK/wrapper_pod_wide.txt" || true
  kubectl -n "$NS" logs "$WRAP_POD" --tail=300 > "$BK/wrapper_logs.txt" || true
  kubectl -n "$NS" describe pod "$WRAP_POD" > "$BK/wrapper_describe.txt" || true

  WRAP_IP="$(kubectl -n "$NS" get pod "$WRAP_POD" -o jsonpath='{.status.podIP}' || true)"
  echo "[+] Wrapper podIP: ${WRAP_IP:-<none>}" | tee "$BK/wrapper_podip.txt"

  if [[ -n "$WRAP_IP" ]]; then
    echo
    echo "[+] Direct /health to podIP (bypass service): http://${WRAP_IP}:${WRAPPER_PORT}/health"
    kubectl -n "$NS" run "curltest-wrapip-$(date +%s)" --rm -i --restart=Never --image="$CURL_IMAGE" -- \
      sh -lc "curl -sS -i --connect-timeout ${CONNECT_TIMEOUT} --max-time ${MAX_TIME_HEALTH} http://${WRAP_IP}:${WRAPPER_PORT}/health" \
      | tee "$BK/health_podip.txt" || true
  fi
fi

############################################
# 3) SERVICE /health with retries
############################################
WRAP_FQDN="${WRAPPER_SVC}.${NS}.svc.cluster.local"
echo
echo "[+] /health via service FQDN=${WRAP_FQDN}:${WRAPPER_PORT} (retries=${HEALTH_RETRIES})"
for i in $(seq 1 "$HEALTH_RETRIES"); do
  set +e
  out="$(kubectl -n "$NS" run "curltest-health-${i}-$(date +%s)" --rm -i --restart=Never --image="$CURL_IMAGE" -- \
    sh -lc "curl -sS -i --connect-timeout ${CONNECT_TIMEOUT} --max-time ${MAX_TIME_HEALTH} http://${WRAP_FQDN}:${WRAPPER_PORT}/health" 2>&1)"
  rc=$?
  set -e

  printf "%s\n" "$out" > "$BK/health_svc_attempt_${i}.txt"
  if [[ $rc -eq 0 ]]; then
    echo "[+] OK on attempt $i"
    break
  fi
  echo "[!] attempt $i failed (rc=$rc) -> sleep ${HEALTH_SLEEP}s"
  sleep "$HEALTH_SLEEP"
done

############################################
# 4) Fetch token
############################################
echo
echo "[+] Fetch API token"
TOKEN="$(kubectl -n "$NS" get secret "$SECRET_NAME" -o "jsonpath={.data.${SECRET_KEY}}" | base64 -d || true)"
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: token empty (secret ${SECRET_NAME} key ${SECRET_KEY})" >&2
  exit 1
fi
echo "Token length: ${#TOKEN}" | tee "$BK/token_len.txt"

############################################
# 5) Create session (best effort) + detect newest pw service
############################################
echo
echo "[+] Snapshot pw-* services BEFORE"
kubectl -n "$NS" get svc -o name | grep '^service/pw-' | sed 's#service/##' | sort > "$BK/pw_svcs_before.txt" || true

echo "[+] POST /sessions (best-effort; may timeout if pw not ready)"
set +e
RESP="$(kubectl -n "$NS" run "curltest-sessions-$(date +%s)" --rm -i --restart=Never --image="$CURL_IMAGE" -- \
  sh -lc "curl -sS -i --connect-timeout ${CONNECT_TIMEOUT} --max-time ${MAX_TIME_SESSIONS} \
    -H 'X-API-Token: ${TOKEN}' -H 'Content-Type: application/json' \
    -X POST http://${WRAP_FQDN}:${WRAPPER_PORT}/sessions -d '{}'" 2>&1)"
RC=$?
set -e
printf "%s\n" "$RESP" > "$BK/sessions_response_raw.txt"
echo "[+] /sessions rc=$RC (saved $BK/sessions_response_raw.txt)"

echo
echo "[+] Snapshot pw-* services AFTER"
kubectl -n "$NS" get svc -o name | grep '^service/pw-' | sed 's#service/##' | sort > "$BK/pw_svcs_after.txt" || true

NEW_SVC="$(comm -13 "$BK/pw_svcs_before.txt" "$BK/pw_svcs_after.txt" | tail -n 1 || true)"
echo "[+] New pw svc detected: ${NEW_SVC:-<none>}" | tee "$BK/new_pw_svc.txt"

############################################
# 6) If pw svc exists, inspect endpointslice + pod + logs
############################################
if [[ -n "$NEW_SVC" ]]; then
  echo
  echo "[+] pw service YAML: $NEW_SVC"
  kubectl -n "$NS" get svc "$NEW_SVC" -o yaml > "$BK/${NEW_SVC}_svc.yaml" || true

  echo "[+] pw endpointslice for svc: $NEW_SVC"
  kubectl -n "$NS" get endpointslice -l "kubernetes.io/service-name=${NEW_SVC}" -o yaml > "$BK/${NEW_SVC}_endpointslice.yaml" || true

  # Extract sid label from endpointslice (best effort)
  SID="$(awk '/sid: /{print $2; exit}' "$BK/${NEW_SVC}_endpointslice.yaml" 2>/dev/null || true)"
  echo "[+] sid(from endpointslice): ${SID:-<unknown>}" | tee "$BK/${NEW_SVC}_sid.txt"

  # Try find pod: either pw-<sid> or targetRef in endpointslice
  PW_POD=""
  if [[ -n "$SID" ]]; then
    PW_POD="pw-${SID}"
  fi

  if [[ -n "$PW_POD" ]] && kubectl -n "$NS" get pod "$PW_POD" >/dev/null 2>&1; then
    echo "[+] pw pod found: $PW_POD"
  else
    # fallback: parse targetRef.name
    PW_POD="$(awk '/name: pw-/{print $2; exit}' "$BK/${NEW_SVC}_endpointslice.yaml" 2>/dev/null || true)"
  fi

  echo "[+] pw pod detected: ${PW_POD:-<none>}" | tee "$BK/${NEW_SVC}_pw_pod.txt"

  if [[ -n "$PW_POD" ]] && kubectl -n "$NS" get pod "$PW_POD" >/dev/null 2>&1; then
    kubectl -n "$NS" get pod "$PW_POD" -o wide > "$BK/${PW_POD}_wide.txt" || true
    kubectl -n "$NS" get pod "$PW_POD" -o yaml > "$BK/${PW_POD}.yaml" || true
    kubectl -n "$NS" logs "$PW_POD" --all-containers --tail=400 > "$BK/${PW_POD}_logs.txt" || true
    kubectl -n "$NS" describe pod "$PW_POD" > "$BK/${PW_POD}_describe.txt" || true
    echo
    echo "[+] pw container command/args excerpt:"
    sed -n '/containers:/,/securityContext:/p' "$BK/${PW_POD}.yaml" | sed -n '1,220p' || true
  else
    echo "[!] pw pod not found (may have already terminated or never created)"
  fi
fi

echo
echo "[OK] Triage bundle saved in: $BK"
