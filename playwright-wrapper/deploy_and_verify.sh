#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# deploy_and_verify.sh - Version Optimisée
###############################################################################

# ---- Paths
WRAPPER_REPO_ROOT="${WRAPPER_REPO_ROOT:-$PWD}"
GITOPS_DIR="${GITOPS_DIR:-$HOME/n8n-gitops}"
VALUES_PROD="${VALUES_PROD:-$GITOPS_DIR/apps/n8n/values-prod.yaml}"
CHART_WRAPPER_VALUES="${CHART_WRAPPER_VALUES:-$GITOPS_DIR/charts/n8n/charts/playwright-wrapper/values.yaml}"

# ---- K8s / kind
KIND_NAME="${KIND_NAME:-pw}"
NAMESPACE="${NAMESPACE:-n8n-prod}"
WRAPPER_DEPLOY="${WRAPPER_DEPLOY:-playwright-wrapper}"
N8N_DEPLOY="${N8N_DEPLOY:-n8n-prod}"

# ---- Image naming
IMAGE_REPO="${IMAGE_REPO:-playwright-wrapper}"
TAG_PREFIX="${TAG_PREFIX:-proxy}"
TAG="${TAG:-${TAG_PREFIX}-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${IMAGE_REPO}:${TAG}"

# ---- Behavior toggles
UPDATE_GITOPS="${UPDATE_GITOPS:-true}"
PATCH_LIVE_DEPLOY="${PATCH_LIVE_DEPLOY:-true}"

# ---- Helpers
die() { echo "❌ ERROR: $*" >&2; exit 1; }
info() { echo "[+] $*"; }

require() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

backup_file() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  local ts; ts="$(date +%Y%m%d-%H%M%S)"
  cp -a "$f" "${f}.bak.${ts}"
  info "Backup created: ${f}.bak.${ts}"
}

update_yaml_tag_best_effort() {
  local file="$1"
  [[ -f "$file" ]] || { info "Skip (not found): $file"; return 0; }
  backup_file "$file"

  python3 - <<PY
import re, pathlib
path = pathlib.Path('${file}')
text = path.read_text(encoding="utf-8")
new_repo = '${IMAGE_REPO}'
new_tag  = '${TAG}'
changed = False

# Pass 1: Inline references
text2 = re.sub(r'([\'"])%s:([A-Za-z0-9._-]+)([\'"])' % re.escape(new_repo), 
               r'\1%s:%s\3' % (new_repo, new_tag), text)
if text2 != text:
    text = text2
    changed = True

# Pass 2: YAML blocks repository + tag
pattern = re.compile(r'(^\s*repository\s*:\s*%s\s*$)(.{0,400}?)(^\s*tag\s*:\s*("?)([^"\n]+)\4\s*$)' % re.escape(new_repo), re.MULTILINE | re.DOTALL)
def sub_block(m):
    global changed
    changed = True
    return m.group(1) + m.group(2) + re.sub(r'^(\s*tag\s*:\s*)("?)([^"\n]+)\2\s*$', r'\1"%s"' % new_tag, m.group(3), flags=re.MULTILINE)

text3 = re.sub(pattern, sub_block, text)
if text3 != text:
    text = text3
    changed = True

if changed:
    path.write_text(text, encoding="utf-8")
    print(f"[+] Updated {path} to tag={new_tag}")
else:
    print(f"[=] No patterns found in {path}")
PY
}

###############################################################################
# Execution
###############################################################################
require docker
require kubectl
require kind
require python3

info "Building image: $IMAGE"
docker build -t "$IMAGE" "$WRAPPER_REPO_ROOT"

info "Loading image into kind cluster: $KIND_NAME"
kind load docker-image --name "$KIND_NAME" "$IMAGE"

if [[ "$UPDATE_GITOPS" == "true" ]]; then
  update_yaml_tag_best_effort "$VALUES_PROD"
  update_yaml_tag_best_effort "$CHART_WRAPPER_VALUES"
fi

if [[ "$PATCH_LIVE_DEPLOY" == "true" ]]; then
  info "Patching live deployment..."
  
  # Détection dynamique du nom du conteneur
  CONTAINER_NAME=$(kubectl -n "$NAMESPACE" get deploy "$WRAPPER_DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].name}')
  
  # Patch avec annotation pour forcer le restart (évite le cache K8s)
  kubectl -n "$NAMESPACE" patch deployment "$WRAPPER_DEPLOY" -p \
    "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"deploy-timestamp\":\"$(date +%s)\"}},\"spec\":{\"containers\":[{\"name\":\"$CONTAINER_NAME\",\"image\":\"$IMAGE\"}]}}}}"

  info "Waiting for rollout..."
  kubectl -n "$NAMESPACE" rollout status "deploy/${WRAPPER_DEPLOY}" --timeout=180s
fi

info "Running Verification..."

# Exécution du test via le pod n8n
# On utilise une session ID unique pour éviter les collisions de cache
CHECK_SESSION="verify-$(date +%s)"
RESPONSE=$(kubectl -n "$NAMESPACE" exec "deploy/${N8N_DEPLOY}" -- curl -s -i --max-time 10 \
     -H "Accept: application/json" \
     -H "Content-Type: application/json" \
     -X POST "http://playwright-wrapper:8080/mcp?session=${CHECK_SESSION}" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')

echo "--- Debug Output ---"
echo "$RESPONSE" | head -n 15
echo "--------------------"

if echo "$RESPONSE" | grep -qi "content-type: application/json"; then
  info "✅ VERIFICATION SUCCESS: Header is application/json"
elif echo "$RESPONSE" | grep -qi "content-type: text/event-stream"; then
  die "❌ VERIFICATION FAILED: Still getting text/event-stream. Logic not updated!"
else
  die "❌ VERIFICATION FAILED: Unexpected response or timeout."
fi

info "Deployment and verification finished successfully."
