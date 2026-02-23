#!/usr/bin/env bash
set -euo pipefail

############################################
# CONFIG
############################################
NS="${NS:-n8n-prod}"
DEPLOY="${DEPLOY:-playwright-wrapper}"
SVC_WRAPPER="${SVC_WRAPPER:-playwright-wrapper}"
SECRET_NAME="${SECRET_NAME:-playwright-wrapper-secret}"
SECRET_KEY="${SECRET_KEY:-API_TOKEN}"

KIND_CLUSTER="${KIND_CLUSTER:-pw}"

DOCKER_TAG="${DOCKER_TAG:-playwright-wrapper:fix-shlc-$(date +%Y%m%d-%H%M%S)}"
DOCKERFILE_DIR="${DOCKERFILE_DIR:-.}"

CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.6.0}"

POST_TIMEOUT_SECONDS="${POST_TIMEOUT_SECONDS:-90}"
ROLL_OUT_TIMEOUT_SECONDS="${ROLL_OUT_TIMEOUT_SECONDS:-240}"

OUT_DIR="${OUT_DIR:-./deploy_verify_v2_$(date +%Y%m%d_%H%M%S)}"

############################################
# HELPERS
############################################
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing dependency: $1" >&2; exit 1; }; }

need docker
need kind
need kubectl
need base64
need sed
need awk
need grep
need sort
need tail
need date

mkdir -p "$OUT_DIR"

echo "[+] Output dir: $OUT_DIR"
echo "[+] Namespace=$NS deploy=$DEPLOY wrapper_svc=$SVC_WRAPPER kind_cluster=$KIND_CLUSTER"
echo

############################################
# BUILD + LOAD + DEPLOY
############################################
echo "[+] Build image: $DOCKER_TAG"
docker build -t "$DOCKER_TAG" "$DOCKERFILE_DIR" | tee "$OUT_DIR/docker_build.log"

echo "[+] kind load docker-image $DOCKER_TAG --name $KIND_CLUSTER"
kind load docker-image "$DOCKER_TAG" --name "$KIND_CLUSTER" | tee "$OUT_DIR/kind_load.log"

echo "[+] Deploy image"
kubectl -n "$NS" set image "deploy/$DEPLOY" wrapper="$DOCKER_TAG" | tee "$OUT_DIR/kubectl_set_image.log"
kubectl -n "$NS" rollout restart "deploy/$DEPLOY" | tee "$OUT_DIR/kubectl_rollout_restart.log"
kubectl -n "$NS" rollout status "deploy/$DEPLOY" "--timeout=${ROLL_OUT_TIMEOUT_SECONDS}s" | tee "$OUT_DIR/kubectl_rollout_status.log"

############################################
# HEALTH CHECK
############################################
echo "[+] Verify /health via service"
kubectl -n "$NS" run "curltest-health-$$" --rm -i --restart=Never --image="$CURL_IMAGE" -- \
  sh -lc "set -e; curl -sS -i --connect-timeout 3 --max-time 10 http://${SVC_WRAPPER}.${NS}.svc.cluster.local:8080/health" \
  | tee "$OUT_DIR/health.txt"

############################################
# GET TOKEN
############################################
echo "[+] Fetch API token"
TOKEN="$(kubectl -n "$NS" get secret "$SECRET_NAME" -o "jsonpath={.data.${SECRET_KEY}}" | base64 -d)"
echo "token_len=${#TOKEN}" | tee "$OUT_DIR/token_len.txt"

############################################
# MARK START TIME (for "new" SID only)
############################################
START_EPOCH="$(date -u +%s)"
echo "[+] START_EPOCH=$START_EPOCH (UTC)" | tee "$OUT_DIR/start_epoch.txt"

############################################
# POST /sessions (capture rc + output)
############################################
echo "[+] POST /sessions (timeout=${POST_TIMEOUT_SECONDS}s)"
set +e
kubectl -n "$NS" run "curltest-sessions-$$" --rm -i --restart=Never --image="$CURL_IMAGE" -- \
  sh -lc "set -e; curl -sS -i --connect-timeout 3 --max-time ${POST_TIMEOUT_SECONDS} \
    -H 'X-API-Token: ${TOKEN}' -H 'Content-Type: application/json' \
    -X POST http://${SVC_WRAPPER}.${NS}.svc.cluster.local:8080/sessions -d '{}'" \
  | tee "$OUT_DIR/sessions_raw.txt"
RC="${PIPESTATUS[0]}"
set -e
echo "sessions_kubectl_run_rc=$RC" | tee "$OUT_DIR/sessions_rc.txt"

SID_FROM_BODY="$(grep -Eo '"sessionId"\s*:\s*"[^"]+"' "$OUT_DIR/sessions_raw.txt" | head -n 1 | sed -E 's/.*"sessionId"\s*:\s*"([^"]+)".*/\1/' || true)"
echo "sid_from_body=$SID_FROM_BODY" | tee "$OUT_DIR/sid_from_body.txt"

############################################
# FIND "NEW" SID strictly after START_EPOCH
############################################
echo "[+] Detect NEW EndpointSlices created after START_EPOCH"
# Output format: creationTimestamp sid svcname
kubectl -n "$NS" get endpointslice -l app=pw \
  -o jsonpath='{range .items[*]}{.metadata.creationTimestamp}{" "}{.metadata.labels.sid}{" "}{.metadata.labels.kubernetes\.io/service-name}{"\n"}{end}' \
  > "$OUT_DIR/es_all.txt" || true

# Convert timestamp -> epoch and filter > START_EPOCH
# date -d handles RFC3339 like 2026-02-18T10:30:17Z
awk -v start="$START_EPOCH" '
  function to_epoch(ts,  cmd, epoch) {
    cmd = "date -u -d \"" ts "\" +%s 2>/dev/null"
    cmd | getline epoch
    close(cmd)
    return epoch
  }
  {
    ts=$1; sid=$2; svc=$3;
    e=to_epoch(ts);
    if (e != "" && e > start) {
      print e, ts, sid, svc
    }
  }
' "$OUT_DIR/es_all.txt" | sort -n > "$OUT_DIR/es_new.txt" || true

echo "[+] New EndpointSlices (if any):"
tail -n 20 "$OUT_DIR/es_new.txt" | tee "$OUT_DIR/es_new_tail.txt" || true

NEW_SID=""
NEW_SVC=""
if [[ -n "$SID_FROM_BODY" ]]; then
  NEW_SID="$SID_FROM_BODY"
  NEW_SVC="pw-${NEW_SID}"
else
  # take newest (last line) from filtered list
  if [[ -s "$OUT_DIR/es_new.txt" ]]; then
    NEW_SID="$(tail -n 1 "$OUT_DIR/es_new.txt" | awk '{print $3}')"
    NEW_SVC="$(tail -n 1 "$OUT_DIR/es_new.txt" | awk '{print $4}')"
  fi
fi

if [[ -z "$NEW_SID" ]]; then
  echo "ERROR: no NEW SID detected after POST /sessions." >&2
  echo "[+] Wrapper logs (tail 200) for clue:"
  kubectl -n "$NS" logs "deploy/$DEPLOY" --tail=200 | tee "$OUT_DIR/wrapper_logs_tail200.txt" || true
  echo "[+] Wrapper pod describe:"
  kubectl -n "$NS" get pod -l "app.kubernetes.io/name=playwright-wrapper,app.kubernetes.io/instance=n8n-prod" -o wide \
    | tee "$OUT_DIR/wrapper_pods.txt" || true
  echo "[+] Namespace events (tail 80):"
  kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 80 | tee "$OUT_DIR/events_tail80.txt" || true
  exit 1
fi

PW_POD="pw-${NEW_SID}"
PW_SVC="${NEW_SVC:-pw-${NEW_SID}}"

echo "[+] Selected NEW_SID=$NEW_SID"
echo "new_sid=$NEW_SID" | tee "$OUT_DIR/selected_sid.txt"
echo "pw_pod=$PW_POD pw_svc=$PW_SVC" | tee "$OUT_DIR/selected_targets.txt"

############################################
# WAIT POD + INSPECT
############################################
echo "[+] Wait for pw pod to appear (best-effort 60s)"
for i in $(seq 1 60); do
  if kubectl -n "$NS" get pod "$PW_POD" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "[+] pw pod status"
kubectl -n "$NS" get pod "$PW_POD" -o wide | tee "$OUT_DIR/pw_pod_status.txt" || true

echo "[+] Extract pw command/args (jsonpath)"
kubectl -n "$NS" get pod "$PW_POD" -o jsonpath='{.spec.containers[0].command}{"\n"}{.spec.containers[0].args}{"\n"}' \
  | tee "$OUT_DIR/pw_command_args.txt" || true

echo "[+] pw logs (tail 200)"
kubectl -n "$NS" logs "$PW_POD" --all-containers --tail=200 | tee "$OUT_DIR/pw_logs.txt" || true

############################################
# VERIFY: args must be compacted for sh -lc
############################################
echo "[+] Verify sh -lc compaction (args must be exactly 2 items: [-lc, '<cmd string>'])"
ARGS_LEN="$(kubectl -n "$NS" get pod "$PW_POD" -o jsonpath='{len(.spec.containers[0].args)}' 2>/dev/null || echo "")"
echo "args_len=$ARGS_LEN" | tee "$OUT_DIR/args_len.txt"

if [[ "$ARGS_LEN" != "2" ]]; then
  echo "ERROR: args_len=$ARGS_LEN, expected 2. sh -lc is NOT compacted => still broken." >&2
  exit 1
fi

CMDSTR="$(kubectl -n "$NS" get pod "$PW_POD" -o jsonpath='{.spec.containers[0].args[1]}' 2>/dev/null || true)"
echo "cmdstr=$CMDSTR" | tee "$OUT_DIR/cmdstr.txt"

if [[ "$CMDSTR" != *"ulimit -n 65535"* ]] || [[ "$CMDSTR" != *"&&"* ]] || [[ "$CMDSTR" != *"exec node cli.js"* ]]; then
  echo "ERROR: compacted string does not look right (missing expected fragments)." >&2
  exit 1
fi

echo "[OK] sh -lc compaction looks correct."

############################################
# PROBE MCP (best-effort)
############################################
echo "[+] Probe MCP via service (GET /mcp, 5s)"
kubectl -n "$NS" run "curltest-mcp-${NEW_SID}-$$" --rm -i --restart=Never --image="$CURL_IMAGE" -- \
  sh -lc "set -e; curl -sS -i --connect-timeout 2 --max-time 5 http://${PW_SVC}.${NS}.svc.cluster.local:8933/mcp | head -n 20" \
  | tee "$OUT_DIR/mcp_probe.txt" || true

echo
echo "[DONE] Bundle: $OUT_DIR"
echo "Key outputs:"
echo "  - $OUT_DIR/sessions_raw.txt"
echo "  - $OUT_DIR/es_new_tail.txt"
echo "  - $OUT_DIR/pw_command_args.txt"
echo "  - $OUT_DIR/pw_logs.txt"
echo "  - $OUT_DIR/cmdstr.txt"

