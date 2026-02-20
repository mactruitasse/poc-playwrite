#!/usr/bin/env bash
set -euo pipefail

############################################
# Variables configurables
############################################
NS="${NS:-n8n-prod}"
HOST="${HOST:-n8n.localhost}"
SERVICE_NAME="${SERVICE_NAME:-n8n-prod}"
SERVICE_PORT="${SERVICE_PORT:-5678}"
INGRESS_NAME="${INGRESS_NAME:-n8n-prod}"
INGRESS_CLASS="${INGRESS_CLASS:-nginx}"
PATH_PREFIX="${PATH_PREFIX:-/}"
OUT_DIR="${OUT_DIR:-./debug-$(date +%Y%m%d-%H%M%S)}"

############################################
# Pré-requis
############################################
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERREUR: '$1' introuvable"; exit 1; }; }
need kubectl
need curl
need awk
need sed
need grep

mkdir -p "$OUT_DIR"

echo "== Contexte kubectl =="
kubectl config current-context | tee "$OUT_DIR/kube-context.txt"
kubectl version | tee "$OUT_DIR/kube-version.txt" || true
echo

echo "== Vérifications préalables =="
kubectl get ns "$NS" >/dev/null
kubectl -n "$NS" get svc "$SERVICE_NAME" >/dev/null
kubectl get ingressclass "$INGRESS_CLASS" >/dev/null
echo "OK: namespace/service/ingressclass existent"
echo

echo "== Sauvegardes (avant) =="
kubectl -n "$NS" get svc "$SERVICE_NAME" -o yaml > "$OUT_DIR/svc-${SERVICE_NAME}.yaml"
kubectl -n "$NS" get ingress -o yaml > "$OUT_DIR/ingress-before.yaml" || true
echo

############################################
# Vérifier que n8n répond bien en direct
############################################
echo "== Test direct: port-forward service =="
PF_LOG="$OUT_DIR/portforward.log"
( kubectl -n "$NS" port-forward "svc/${SERVICE_NAME}" 18080:"$SERVICE_PORT" >"$PF_LOG" 2>&1 & echo $! > "$OUT_DIR/pf.pid" )
sleep 1

set +e
HTTP_SVC="$(curl -sS -o "$OUT_DIR/curl-service.body" -w "%{http_code}" "http://127.0.0.1:18080${PATH_PREFIX}")"
set -e
echo "HTTP via Service sur ${PATH_PREFIX}: $HTTP_SVC"
kill "$(cat "$OUT_DIR/pf.pid")" >/dev/null 2>&1 || true
echo

if [[ "$HTTP_SVC" != "200" && "$HTTP_SVC" != "301" && "$HTTP_SVC" != "302" ]]; then
  echo "ERREUR: le service ne répond pas comme attendu (code $HTTP_SVC). Corrige d'abord le service/app."
  exit 1
fi

############################################
# Créer / Mettre à jour l'Ingress
############################################
echo "== Appliquer Ingress =="
cat > "$OUT_DIR/ingress.yaml" <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${INGRESS_NAME}
  namespace: ${NS}
spec:
  ingressClassName: ${INGRESS_CLASS}
  rules:
  - host: ${HOST}
    http:
      paths:
      - path: ${PATH_PREFIX}
        pathType: Prefix
        backend:
          service:
            name: ${SERVICE_NAME}
            port:
              number: ${SERVICE_PORT}
EOF

kubectl apply -f "$OUT_DIR/ingress.yaml" | tee "$OUT_DIR/kubectl-apply.txt"
echo

echo "== Attendre que l'Ingress soit visible =="
kubectl -n "$NS" get ingress "$INGRESS_NAME" -o wide | tee "$OUT_DIR/ingress-wide.txt"
kubectl -n "$NS" describe ingress "$INGRESS_NAME" | tee "$OUT_DIR/ingress-describe.txt"
echo

############################################
# Vérifier via ingress-nginx (Host header)
############################################
echo "== Test via Ingress NGINX (Host header) =="
# Dans kind, l'ingress-nginx écoute souvent sur localhost:80 (via extraPortMappings),
# donc on teste http://127.0.0.1 avec Host.
set +e
HTTP_ING="$(curl -sS -o "$OUT_DIR/curl-ingress.body" -w "%{http_code}" -H "Host: ${HOST}" "http://127.0.0.1${PATH_PREFIX}")"
set -e
echo "HTTP via Ingress sur 127.0.0.1 Host=${HOST} path=${PATH_PREFIX}: $HTTP_ING"
echo

echo "== Vérification finale =="
if [[ "$HTTP_ING" == "200" || "$HTTP_ING" == "301" || "$HTTP_ING" == "302" ]]; then
  echo "OK: l'Ingress route vers n8n."
else
  echo "KO: toujours $HTTP_ING."
  echo "Actions à vérifier (preuves dans $OUT_DIR):"
  echo " - ingress-describe.txt: backend/service/port corrects"
  echo " - ingress-wide.txt: adressage (ADDRESS) éventuellement vide en kind, mais routing doit marcher via controller"
  echo " - curl-ingress.body: contenu exact de la réponse"
fi

echo
echo "Artifacts: $OUT_DIR"
