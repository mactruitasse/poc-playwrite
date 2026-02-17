#!/usr/bin/env bash
set -euo pipefail

# Unstick "old replicas pending termination" for playwright-wrapper.
# Strategy:
# - Show pods/rs
# - Force-delete ALL pods of the deployment (label selector) to let controller converge
# - If still stuck, scale deploy to 0 then back to 1
# - Wait for rollout and verify readiness
#
# Usage:
#   ./unstick_wrapper_oldreplica.sh
#
# Optional env:
#   NAMESPACE=default
#   DEPLOY=playwright-wrapper
#   LABEL_SELECTOR='app.kubernetes.io/name=playwright-wrapper'
#   TIMEOUT=180s
#   SCALE_RESET=1  (default 1)

NAMESPACE="${NAMESPACE:-default}"
DEPLOY="${DEPLOY:-playwright-wrapper}"
LABEL_SELECTOR="${LABEL_SELECTOR:-app.kubernetes.io/name=playwright-wrapper}"
TIMEOUT="${TIMEOUT:-180s}"
SCALE_RESET="${SCALE_RESET:-1}"

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }; }
need_cmd kubectl

ts="$(date +%Y%m%d%H%M%S)"

echo "[0/7] Backup deploy/rs/pods -> /tmp"
kubectl -n "$NAMESPACE" get deploy "$DEPLOY" -o yaml > "/tmp/${DEPLOY}.deploy.bak.${ts}.yaml" || true
kubectl -n "$NAMESPACE" get rs -l "$LABEL_SELECTOR" -o yaml > "/tmp/${DEPLOY}.rs.bak.${ts}.yaml" || true
kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o yaml > "/tmp/${DEPLOY}.pods.bak.${ts}.yaml" || true

echo "[1/7] Current deploy image/pullPolicy"
kubectl -n "$NAMESPACE" get deploy "$DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[0].image}{" "}{.spec.template.spec.containers[0].imagePullPolicy}{"\n"}' || true

echo "[2/7] Pods (wrapper)"
kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o wide || true

echo "[3/7] ReplicaSets (wrapper) newest last"
kubectl -n "$NAMESPACE" get rs -l "$LABEL_SELECTOR" \
  -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas,IMAGE:.spec.template.spec.containers[0].image,CREATED:.metadata.creationTimestamp \
  --sort-by=.metadata.creationTimestamp || true

echo "[4/7] Force-delete ALL wrapper pods (to clear 'old replica pending termination')"
PODS="$(kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' || true)"
if [[ -n "${PODS:-}" ]]; then
  echo "$PODS" | while read -r p; do
    [[ -n "$p" ]] || continue
    kubectl -n "$NAMESPACE" delete pod "$p" --force --grace-period=0 || true
  done
else
  echo "No pods found for selector: $LABEL_SELECTOR"
fi

echo "[5/7] Wait a moment and check rollout"
sleep 3
if ! kubectl -n "$NAMESPACE" rollout status "deploy/${DEPLOY}" --timeout=20s; then
  echo "[WARN] Still not converged quickly."
  if [[ "$SCALE_RESET" == "1" ]]; then
    echo "[6/7] Scale reset: scale to 0 then back to 1"
    kubectl -n "$NAMESPACE" scale "deploy/${DEPLOY}" --replicas=0
    kubectl -n "$NAMESPACE" rollout status "deploy/${DEPLOY}" --timeout=60s || true
    kubectl -n "$NAMESPACE" scale "deploy/${DEPLOY}" --replicas=1
  fi
fi

echo "[7/7] Final rollout wait + verification"
kubectl -n "$NAMESPACE" rollout status "deploy/${DEPLOY}" --timeout="$TIMEOUT" || true
kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" -o wide || true

NEWPOD="$(kubectl -n "$NAMESPACE" get pods -l "$LABEL_SELECTOR" --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)"
echo "NEWPOD=${NEWPOD:-<none>}"
if [[ -n "${NEWPOD:-}" ]]; then
  echo -n "READY="
  kubectl -n "$NAMESPACE" get pod "$NEWPOD" -o jsonpath='{.status.containerStatuses[0].ready}'; echo
  echo "- Events (new pod):"
  kubectl -n "$NAMESPACE" describe pod "$NEWPOD" | sed -n '/Events:/,$p' || true
  echo "- Logs (new pod):"
  kubectl -n "$NAMESPACE" logs "$NEWPOD" --tail=120 || true
fi

echo "Done."
