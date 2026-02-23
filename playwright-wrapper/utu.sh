#!/usr/bin/env bash
set -euo pipefail

############################################
# CONFIG
############################################
NS="${NS:-n8n-prod}"
DEPLOY="${DEPLOY:-playwright-wrapper}"
CONTAINER="${CONTAINER:-wrapper}"
KIND_CLUSTER="${KIND_CLUSTER:-pw}"

# Image tag for local test
IMG_TAG="${IMG_TAG:-playwright-wrapper:fix-shlc-$(date +%Y%m%d-%H%M%S)}"

# Files
MAIN_PY="${MAIN_PY:-app/main.py}"

# Curl/test images
CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.6.0}"
WRAPPER_FQDN="${WRAPPER_FQDN:-playwright-wrapper.${NS}.svc.cluster.local:8080}"

# Timeouts
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_SLEEP="${HEALTH_SLEEP:-2}"
SESSIONS_TIMEOUT="${SESSIONS_TIMEOUT:-60}"

BK_DIR="./patch_shlc_backup_$(date +%Y%m%d_%H%M%S)"

############################################
# Helpers
############################################
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing dependency: $1" >&2; exit 1; }; }
need sed
need awk
need grep
need docker
need kubectl
need kind

mkdir -p "$BK_DIR"
cp -a "$MAIN_PY" "$BK_DIR/main.py.bak"

echo "[+] Backup to $BK_DIR"

############################################
# Patch _playwright_container_spec()
############################################
echo "[+] Patching $MAIN_PY (fix sh -lc compaction without quoting &&)"

python3 - <<'PY'
import re
from pathlib import Path

path = Path("app/main.py")
txt = path.read_text(encoding="utf-8")

# Find function block
m = re.search(r"def _playwright_container_spec\(\)\s*->\s*client\.V1Container:\n(?P<body>(?:[ \t].*\n)+)", txt)
if not m:
    raise SystemExit("ERROR: cannot find _playwright_container_spec()")

body = m.group("body")

# We will replace the internal compaction logic with a safer version.
# Target: find the if cmd in ("sh", "/bin/sh") ... block and replace it entirely.
block_re = re.compile(
    r"(?ms)^\s*# IMPORTANT:\n\s*# If PLAYWRIGHT_COMMAND=sh.*?^\s*return client\.V1Container\(",
)

bm = block_re.search(body)
if not bm:
    raise SystemExit("ERROR: could not find the compaction/comment block to replace (format changed)")

prefix = body[:bm.start()]
suffix = body[bm.end()-len("return client.V1Container("):]  # keep from return line

replacement = """    # IMPORTANT:
    # If PLAYWRIGHT_COMMAND=sh and PLAYWRIGHT_ARGS starts with -c/-lc, shlex.split()
    # yields many tokens, but sh expects the command after -c/-lc as ONE string.
    #
    # Example (bad):
    #   command: ["sh"]
    #   args: ["-lc","ulimit","-n","65535","&&","exec","node",...]
    #
    # Example (good):
    #   command: ["sh"]
    #   args: ["-lc","ulimit -n 65535 && exec node ..."]
    #
    # Also, never keep quotes around shell operators like &&.
    if cmd in ("sh", "/bin/sh") and args:
        if args[0] in ("-c", "-lc") and len(args) > 2:
            # Rebuild a clean shell command string.
            # We only "normalize" a few known tokens; we avoid adding extra quoting.
            toks = []
            for t in args[1:]:
                if t == "'&&'" or t == '"&&"' or t == "&&":
                    toks.append("&&")
                elif t == "'*'" or t == '"*"' or t == "*":
                    toks.append("*")
                else:
                    toks.append(t)
            shell_cmd = " ".join(toks).strip()
            args = [args[0], shell_cmd]

    return client.V1Container(
"""

new_body = prefix + replacement + suffix
new_txt = txt[:m.start("body")] + new_body + txt[m.end("body"):]

path.write_text(new_txt, encoding="utf-8")
PY

echo "[+] python3 syntax check"
python3 -m py_compile "$MAIN_PY"

echo "[+] Show patched function excerpt"
nl -ba "$MAIN_PY" | sed -n '175,235p'

############################################
# Build + kind load + rollout
############################################
echo "[+] Build image: $IMG_TAG"
docker build -t "$IMG_TAG" .

echo "[+] kind load docker-image $IMG_TAG --name $KIND_CLUSTER"
kind load docker-image "$IMG_TAG" --name "$KIND_CLUSTER"

echo "[+] Deploy image to $NS deploy/$DEPLOY container=$CONTAINER"
kubectl -n "$NS" set image "deploy/${DEPLOY}" "${CONTAINER}=${IMG_TAG}"
kubectl -n "$NS" rollout restart "deploy/${DEPLOY}"
kubectl -n "$NS" rollout status "deploy/${DEPLOY}" --timeout=240s

############################################
# Verify /health
############################################
echo "[+] Wait /health via service: http://${WRAPPER_FQDN}/health"
ok=0
for i in $(seq 1 "$HEALTH_RETRIES"); do
  set +e
  out="$(kubectl -n "$NS" run "curltest-health-$i-$(date +%s)" --rm -i --restart=Never --image="$CURL_IMAGE" -- \
    sh -lc "curl -sS -i --connect-timeout 5 --max-time 10 http://${WRAPPER_FQDN}/health | head -n 20" 2>&1)"
  rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    echo "$out"
    ok=1
    break
  fi
  echo "[!] health attempt $i failed (rc=$rc); sleep ${HEALTH_SLEEP}s"
  sleep "$HEALTH_SLEEP"
done
if [[ $ok -ne 1 ]]; then
  echo "ERROR: wrapper health never succeeded" >&2
  echo "Rollback files in $BK_DIR"
  exit 1
fi

############################################
# Create a session and inspect created pw pod args
############################################
echo "[+] Fetch API token"
TOKEN="$(kubectl -n "$NS" get secret playwright-wrapper-secret -o jsonpath='{.data.API_TOKEN}' | base64 -d)"
if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: empty API_TOKEN" >&2
  exit 1
fi

echo "[+] POST /sessions (timeout=${SESSIONS_TIMEOUT}s)"
RESP="$(kubectl -n "$NS" run "curltest-sessions-$(date +%s)" --rm -i --restart=Never --image="$CURL_IMAGE" -- \
  sh -lc "curl -sS --connect-timeout 5 --max-time ${SESSIONS_TIMEOUT} \
    -H 'X-API-Token: ${TOKEN}' -H 'Content-Type: application/json' \
    -X POST http://${WRAPPER_FQDN}/sessions -d '{}'")" || true

echo "$RESP" | tee "$BK_DIR/sessions_response.json"

SID="$(echo "$RESP" | python3 - <<'PY'
import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get("sessionId",""))
except Exception:
    print("")
PY
)"

if [[ -z "$SID" ]]; then
  echo "[!] No sessionId in response (maybe wrapper still timed out waiting for pw readiness)."
  echo "[!] Bundle: $BK_DIR"
  exit 1
fi

echo "[+] Created SID=$SID"

PW_POD="pw-${SID}"
echo "[+] Inspect pw pod: $PW_POD"
kubectl -n "$NS" get pod "$PW_POD" -o wide | tee "$BK_DIR/pw_pod_get.txt" || true

echo "[+] Dump command/args jsonpath"
kubectl -n "$NS" get pod "$PW_POD" -o jsonpath='{.spec.containers[0].command}{"\n"}{.spec.containers[0].args}{"\n"}' \
  | tee "$BK_DIR/pw_pod_command_args.txt" || true

echo "[+] pw pod logs (tail 200)"
kubectl -n "$NS" logs "$PW_POD" --all-containers --tail=200 | tee "$BK_DIR/pw_pod_logs.txt" || true

echo
echo "[OK] Done. Patch backup: $BK_DIR"
echo "Rollback:"
echo "  cp -a '$BK_DIR/main.py.bak' 'app/main.py'"
