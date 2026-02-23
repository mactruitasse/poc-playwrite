#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# deploy_and_verify.sh
# - Patch app/main.py (ignore httpx.RemoteProtocolError in SSE streams)
# - Build docker image locally
# - kind load docker-image (no registry)
# - Patch GitOps values-prod.yaml (playwright-wrapper image tag)
# - git add/commit/push
# - argocd app sync
# - verify cluster image + quick SSE probe (NO head)
###############################################################################

###############################################################################
# Config (override via env if needed)
###############################################################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WRAPPER_DIR="${WRAPPER_DIR:-$SCRIPT_DIR}"
GITOPS_DIR="${GITOPS_DIR:-/home/aconte/n8n-gitops}"
VALUES_FILE="${VALUES_FILE:-$GITOPS_DIR/apps/n8n/values-prod.yaml}"

KIND_NAME="${KIND_NAME:-pw}"
ARGO_APP="${ARGO_APP:-n8n-prod}"
NAMESPACE="${NAMESPACE:-n8n-prod}"

REPO_NAME="${REPO_NAME:-playwright-wrapper}"

# Optional: set to 0 to skip argocd sync (still does git push)
DO_ARGO_SYNC="${DO_ARGO_SYNC:-1}"

###############################################################################
# Helpers
###############################################################################
die() { echo "ERROR: $*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

###############################################################################
# Preflight
###############################################################################
need_cmd python3
need_cmd docker
need_cmd kind
need_cmd kubectl
need_cmd git
need_cmd awk
need_cmd sed
need_cmd argocd

[[ -d "$WRAPPER_DIR" ]] || die "WRAPPER_DIR not found: $WRAPPER_DIR"
[[ -d "$GITOPS_DIR"  ]] || die "GITOPS_DIR not found: $GITOPS_DIR"
[[ -f "$VALUES_FILE" ]] || die "VALUES_FILE not found: $VALUES_FILE"
[[ -f "$WRAPPER_DIR/app/main.py" ]] || die "main.py not found: $WRAPPER_DIR/app/main.py"

cd "$WRAPPER_DIR"

IMAGE_TAG="1.0.1-fix-sse-remoteprotocol-$(date +%Y%m%d-%H%M%S)"
IMAGE="${REPO_NAME}:${IMAGE_TAG}"

echo "[+] Wrapper dir : $WRAPPER_DIR"
echo "[+] GitOps dir  : $GITOPS_DIR"
echo "[+] Values file : $VALUES_FILE"
echo "[+] Image       : $IMAGE"
echo "[+] kind name   : $KIND_NAME"
echo "[+] Argo app    : $ARGO_APP"
echo "[+] Namespace   : $NAMESPACE"
echo

###############################################################################
# 1) Patch wrapper app/main.py : ignore httpx.RemoteProtocolError in SSE streams
###############################################################################
echo "[+] Patch wrapper app/main.py (ignore RemoteProtocolError in SSE streams)"

python3 - <<'PY'
from __future__ import annotations
from pathlib import Path
import py_compile
import sys
import time

MAIN = Path("app/main.py")
txt = MAIN.read_text(encoding="utf-8")
lines = txt.splitlines(True)

MARK = "Upstream SSE closed abruptly"
if MARK in txt:
    print("[OK] Already patched (marker found).")
    py_compile.compile(str(MAIN), doraise=True)
    sys.exit(0)

def indent_of(s: str) -> str:
    return s[:len(s) - len(s.lstrip(" "))]

bak = Path(f"app/main.py.bak.remoteprotocol.{int(time.time())}")
bak.write_text(txt, encoding="utf-8")
print(f"[OK] Backup -> {bak}")

patched_streams = 0
i = 0
while i < len(lines):
    line = lines[i]
    if "async def _stream():" in line:
        scan_limit = min(len(lines), i + 240)
        try_indent = None
        finally_idx = None

        j = i + 1
        while j < scan_limit:
            lj = lines[j]
            if try_indent is None and lj.lstrip().startswith("try:"):
                try_indent = indent_of(lj)
            elif try_indent is not None:
                if indent_of(lj) == try_indent and lj.lstrip().startswith("finally:"):
                    finally_idx = j
                    break
                if (lj.lstrip().startswith("async def ") or lj.lstrip().startswith("def ")) and indent_of(lj) <= indent_of(line):
                    break
            j += 1

        if try_indent and finally_idx is not None:
            ex = [
                try_indent + "except httpx.RemoteProtocolError as e:\n",
                try_indent + f"    log.warning(\"{MARK}: %s\", e)\n",
                try_indent + "    return\n",
            ]
            lines[finally_idx:finally_idx] = ex
            patched_streams += 1
            i = finally_idx + len(ex) + 1
            continue
    i += 1

if patched_streams == 0:
    raise SystemExit("ERROR: No _stream() try/finally blocks found to patch.")

MAIN.write_text("".join(lines), encoding="utf-8")
py_compile.compile(str(MAIN), doraise=True)
print(f"[OK] Patched main.py: streams_patched={patched_streams}")
PY

echo "[OK] Patch done"
echo

###############################################################################
# 2) Docker build
###############################################################################
echo "[+] Docker build: $IMAGE"
docker build -t "$IMAGE" .
echo "[OK] Docker build done"
echo

###############################################################################
# 3) kind load docker-image
###############################################################################
echo "[+] kind load docker-image: $IMAGE --name $KIND_NAME"
kind load docker-image "$IMAGE" --name "$KIND_NAME"
echo "[OK] kind load done"
echo

###############################################################################
# 4) Patch GitOps values-prod.yaml
###############################################################################
echo "[+] Patch values file"
cd "$GITOPS_DIR"

bak_values="${VALUES_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$VALUES_FILE" "$bak_values"
echo "[OK] Backup -> $bak_values"

python3 - <<PY
from __future__ import annotations
from pathlib import Path

values_path = Path("$VALUES_FILE")
repo = "$REPO_NAME"
tag = "$IMAGE_TAG"

lines = values_path.read_text(encoding="utf-8").splitlines(True)

def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))

out = []
in_root = False
root_indent = None
in_image = False
image_indent = None

for line in lines:
    stripped = line.strip()

    if stripped == "playwright-wrapper:":
        in_root = True
        root_indent = indent_of(line)
        in_image = False
        image_indent = None
        out.append(line)
        continue

    if in_root and stripped.endswith(":") and indent_of(line) <= (root_indent or 0) and stripped != "playwright-wrapper:":
        in_root = False
        in_image = False
        image_indent = None
        root_indent = None
        out.append(line)
        continue

    if in_root and stripped == "image:":
        in_image = True
        image_indent = indent_of(line)
        out.append(line)
        continue

    if in_root and in_image and stripped.endswith(":") and indent_of(line) <= (image_indent or 0) and stripped != "image:":
        in_image = False
        image_indent = None
        out.append(line)
        continue

    if in_root and in_image:
        if stripped.startswith("repository:"):
            prefix = line[:line.find("repository:")]
            out.append(f"{prefix}repository: {repo}\n")
            continue
        if stripped.startswith("tag:"):
            prefix = line[:line.find("tag:")]
            out.append(f"{prefix}tag: {tag}\n")
            continue

    out.append(line)

values_path.write_text("".join(out), encoding="utf-8")
print("OK")
PY

echo "[OK] Updated values-prod.yaml -> repository=$REPO_NAME tag=$IMAGE_TAG"
echo

echo "[+] Diff values file:"
git --no-pager diff -- "$VALUES_FILE" | sed -n '1,200p' || true
echo

###############################################################################
# 5) git add/commit/push
###############################################################################
echo "[+] Git commit+push values file"
cd "$GITOPS_DIR"

branch="$(git rev-parse --abbrev-ref HEAD)"
remote="$(git remote | head -n1 || true)"
[[ -n "$remote" ]] || die "No git remote found in $GITOPS_DIR"

git add "$VALUES_FILE"
if git diff --cached --quiet; then
  echo "[WARN] Nothing staged; values file may already be at desired tag."
else
  git commit -m "Bump playwright-wrapper image to $IMAGE_TAG"
fi

git push "$remote" "$branch"
echo "[OK] Git push done"
echo

###############################################################################
# 6) ArgoCD sync
###############################################################################
if [[ "$DO_ARGO_SYNC" == "1" ]]; then
  echo "[+] ArgoCD sync: $ARGO_APP"
  argocd app sync "$ARGO_APP"
  echo "[OK] ArgoCD sync done"
else
  echo "[SKIP] ArgoCD sync disabled (DO_ARGO_SYNC=0)"
fi
echo

###############################################################################
# 7) Verify cluster image + SSE probe (NO head)
###############################################################################
echo "[+] Verify deployment image in cluster"
kubectl -n "$NAMESPACE" get deploy playwright-wrapper -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}' || true
echo

echo "[+] Wait rollout"
kubectl -n "$NAMESPACE" rollout status deploy/playwright-wrapper --timeout=180s || true
echo

echo "[+] SSE test from n8n pod (10s, no head)"
kubectl -n "$NAMESPACE" exec -it deploy/n8n-prod -- sh -lc '
  curl -sS -N --max-time 10 -v \
    -H "Accept: text/event-stream" \
    "http://playwright-wrapper:8080/sse?workflowId=test-456" || true
' || true

echo
echo "[OK] DONE"
