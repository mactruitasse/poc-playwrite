#!/usr/bin/env bash
set -euo pipefail

# Fix Playwright MCP pods readiness by forcing --host 0.0.0.0 via wrapper env vars
# and update Helm chart accordingly, then redeploy in kind.
#
# Usage:
#   ./fix_playwright_wrapper_host.sh 1.0.1-fix-host
#
# Notes:
# - Assumes your repo layout exactly like:
#   charts/n8n/charts/playwright-wrapper/templates/deployment.yaml
#   charts/n8n/charts/playwright-wrapper/values.yaml
#   apps/n8n/values-prod.yaml
# - Keeps ingress disabled in apps/n8n/values-prod.yaml (appends if missing).
# - Loads the wrapper image into kind (tag you provide).
#
# Optional env vars:
#   RELEASE_NAME=n8n
#   NAMESPACE=default
#   KIND_CLUSTER_NAME=kind
#   DO_GIT_COMMIT=0|1
#   COMMIT_MSG="..."
#   VERIFY=0|1   (default 1)

TAG="${1:-}"
if [[ -z "$TAG" ]]; then
  echo "Usage: $0 <wrapper_image_tag>"
  echo "Example: $0 1.0.1-fix-host"
  exit 1
fi

RELEASE_NAME="${RELEASE_NAME:-n8n}"
NAMESPACE="${NAMESPACE:-default}"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-kind}"
DO_GIT_COMMIT="${DO_GIT_COMMIT:-0}"
COMMIT_MSG="${COMMIT_MSG:-fix: bind playwright mcp on 0.0.0.0}"
VERIFY="${VERIFY:-1}"

DEPLOY_TPL="charts/n8n/charts/playwright-wrapper/templates/deployment.yaml"
WRAP_VALUES="charts/n8n/charts/playwright-wrapper/values.yaml"
APP_VALUES="apps/n8n/values-prod.yaml"

ts="$(date +%Y%m%d%H%M%S)"

need_file() {
  local f="$1"
  [[ -f "$f" ]] || { echo "Missing file: $f" >&2; exit 1; }
}

backup() {
  local f="$1"
  cp -a "$f" "${f}.bak.${ts}"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

need_cmd helm
need_cmd kubectl
need_cmd kind

need_file "$DEPLOY_TPL"
need_file "$WRAP_VALUES"
need_file "$APP_VALUES"

echo "[1/7] Backups"
backup "$DEPLOY_TPL"
backup "$WRAP_VALUES"
backup "$APP_VALUES"

echo "[2/7] Ensure ingress disabled in ${APP_VALUES} (avoid host/path collision)"
if ! grep -qE '^[[:space:]]*ingress:[[:space:]]*$' "$APP_VALUES"; then
  cat >> "$APP_VALUES" <<'YAML'

# Disable ingress in default release to avoid host/path conflict with n8n-prod
ingress:
  enabled: false
YAML
else
  # ingress block exists; ensure enabled:false somewhere inside it
  if ! awk '
    BEGIN{in=0; ok=0}
    /^[[:space:]]*ingress:[[:space:]]*$/ {in=1; next}
    in==1 && /^[^[:space:]]/ {in=0}
    in==1 && /^[[:space:]]*enabled:[[:space:]]*false[[:space:]]*$/ {ok=1}
    END{exit ok?0:1}
  ' "$APP_VALUES"; then
    # append override at end (simple and effective)
    cat >> "$APP_VALUES" <<'YAML'

# Force-disable ingress (override)
ingress:
  enabled: false
YAML
  fi
fi

echo "[3/7] Update wrapper chart values: set image tag and playwright args with --host 0.0.0.0"
# Update (or add) env.playwrightCommand / env.playwrightArgs and image.tag in wrapper values
python3 - <<'PY'
import re
from pathlib import Path

tag = Path(".").resolve()
TAG = __import__("os").environ["TAG"] if "TAG" in __import__("os").environ else None
if TAG is None:
    raise SystemExit("TAG env missing")

values_path = Path("charts/n8n/charts/playwright-wrapper/values.yaml")
txt = values_path.read_text(encoding="utf-8")

# Ensure image.tag is set to TAG (best-effort text update)
# Replace first occurrence of "tag:" under image: if present
def set_image_tag(s: str) -> str:
    # try to find "image:\n  ...\n  tag: ..."
    pat = re.compile(r"(^image:\s*\n(?:^[ \t].*\n)*?^[ \t]*tag:\s*).*$", re.M)
    if pat.search(s):
        return pat.sub(rf"\1\"{TAG}\"", s, count=1)
    # else append a minimal image block
    return s.rstrip() + f"\n\nimage:\n  repository: playwright-wrapper\n  tag: \"{TAG}\"\n"

txt2 = set_image_tag(txt)

# Ensure env: block contains playwrightCommand/playwrightArgs
def ensure_env_keys(s: str) -> str:
    cmd_line = '  playwrightCommand: "node"'
    args_line = '  playwrightArgs: "cli.js --headless --browser chromium --no-sandbox --host 0.0.0.0 --port 8933 --shared-browser-context"'

    if re.search(r"^env:\s*$", s, re.M):
        # env exists; add missing keys
        if not re.search(r"^\s*playwrightCommand:\s*", s, re.M):
            s = re.sub(r"^env:\s*$", lambda m: m.group(0) + "\n" + cmd_line, s, count=1, flags=re.M)
        if not re.search(r"^\s*playwrightArgs:\s*", s, re.M):
            s = re.sub(r"^env:\s*$", lambda m: m.group(0) + "\n" + args_line, s, count=1, flags=re.M)
        return s

    # no env: block; append one
    return s.rstrip() + "\n\nenv:\n" + cmd_line + "\n" + args_line + "\n"

txt3 = ensure_env_keys(txt2)

values_path.write_text(txt3, encoding="utf-8")
print("Updated", values_path)
PY
TAG="$TAG" python3 -c "pass" >/dev/null 2>&1 || true

# Export TAG for the python snippet above (portable)
export TAG

echo "[4/7] Update wrapper deployment template to pass PLAYWRIGHT_COMMAND/ARGS env vars"
# Insert env vars after PW_MCP_PORT if not present
if ! grep -q 'name: PLAYWRIGHT_COMMAND' "$DEPLOY_TPL"; then
  python3 - <<'PY'
from pathlib import Path

p = Path("charts/n8n/charts/playwright-wrapper/templates/deployment.yaml")
s = p.read_text(encoding="utf-8").splitlines(True)

out = []
inserted = False
for line in s:
    out.append(line)
    if (not inserted) and ("name: PW_MCP_PORT" in line):
        # insert after the current env var block entry (value line follows)
        continue
    if (not inserted) and ("name: PW_MCP_PORT" in line):
        pass

# second pass: find the "value: {{ .Values.env.playwrightMcpPort | quote }}" line and insert after it
out2 = []
inserted = False
for i, line in enumerate(s):
    out2.append(line)
    if (not inserted) and ("name: PW_MCP_PORT" in line):
        # expect next line is value: ...
        continue
    if (not inserted) and ("value: {{ .Values.env.playwrightMcpPort" in line):
        out2.append("            - name: PLAYWRIGHT_COMMAND\n")
        out2.append("              value: {{ .Values.env.playwrightCommand | quote }}\n")
        out2.append("            - name: PLAYWRIGHT_ARGS\n")
        out2.append("              value: {{ .Values.env.playwrightArgs | quote }}\n")
        inserted = True

if not inserted:
    raise SystemExit("Could not find PW_MCP_PORT env var value line to insert after. Please insert manually.")

p.write_text("".join(out2), encoding="utf-8")
print("Updated", p)
PY
else
  echo "  - PLAYWRIGHT_COMMAND already present, skipping template change"
fi

echo "[5/7] Load wrapper image into kind"
kind load docker-image "playwright-wrapper:${TAG}" --name "${KIND_CLUSTER_NAME}"

echo "[6/7] Helm upgrade in namespace ${NAMESPACE}"
helm upgrade --install "${RELEASE_NAME}" charts/n8n -n "${NAMESPACE}" -f "${APP_VALUES}"

echo "Waiting for rollout..."
kubectl -n "${NAMESPACE}" rollout status deploy/playwright-wrapper

echo "[7/7] Verify wrapper image"
kubectl -n "${NAMESPACE}" get deploy playwright-wrapper -o jsonpath='{.spec.template.spec.containers[0].image}'; echo

if [[ "${VERIFY}" == "1" ]]; then
  echo
  echo "[VERIFY] Create a session and check the latest pw-* pod args include --host 0.0.0.0"
  TOKEN="$(kubectl -n "${NAMESPACE}" get secret playwright-wrapper-secret -o jsonpath='{.data.API_TOKEN}' | base64 -d)"

  kubectl -n "${NAMESPACE}" run "curltest-${ts}" --rm -i --restart=Never --image=curlimages/curl:8.6.0 -- \
    sh -lc "curl -sS -i --max-time 120 \
      -H 'X-API-Token: ${TOKEN}' \
      -H 'Content-Type: application/json' \
      -X POST http://playwright-wrapper:8080/sessions \
      -d '{}'" || true

  NEWPOD="$(kubectl -n "${NAMESPACE}" get pods --sort-by=.metadata.creationTimestamp -o name \
    | sed -n 's|pod/||p' | grep '^pw-' | tail -n1 || true)"

  if [[ -n "${NEWPOD}" ]]; then
    echo "Latest pw pod: ${NEWPOD}"
    kubectl -n "${NAMESPACE}" get pod "${NEWPOD}" -o yaml | sed -n '1,140p' | sed -n '/args:/,/imagePullPolicy:/p' || true
    echo -n "Ready: "
    kubectl -n "${NAMESPACE}" get pod "${NEWPOD}" -o jsonpath='{.status.containerStatuses[0].ready}'; echo
    echo "Logs:"
    kubectl -n "${NAMESPACE}" logs "${NEWPOD}" --tail=20 || true
  else
    echo "[WARN] No pw-* pod found."
  fi
fi

if [[ "${DO_GIT_COMMIT}" == "1" ]]; then
  if command -v git >/dev/null 2>&1; then
    git add "$DEPLOY_TPL" "$WRAP_VALUES" "$APP_VALUES" || true
    git commit -m "${COMMIT_MSG}" || true
    echo "Committed."
  else
    echo "[WARN] git not found; skipping commit."
  fi
fi

echo
echo "Done. Backups: *.bak.${ts}"
