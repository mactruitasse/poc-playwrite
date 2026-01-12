POD="n8n-debug-$(date +%s)"
IMG="n8n/n8n-playwright:2.2.5-pw0.2.21-pwbase"

kubectl -n n8n-prod run "$POD" --restart=Never --image="$IMG" --command -- sh -lc '
set -eux
echo "PATH=$PATH"
node -v
npm -v || true
npx -v || true
echo "=== /usr/lib/node_modules/n8n/dist/modules ==="
ls -la /usr/lib/node_modules/n8n/dist/modules || true
echo "=== /usr/local/lib/node_modules/n8n/dist/modules ==="
ls -la /usr/local/lib/node_modules/n8n/dist/modules || true
'

# Attendre que le pod ne soit plus en Pending (ContainerCreating)
while true; do
  PHASE="$(kubectl -n n8n-prod get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  [ "$PHASE" != "Pending" ] && [ -n "$PHASE" ] && break
  sleep 1
done

# Si ça reste bloqué, les EVENTS expliquent pourquoi (pull image, mount, sandbox, etc.)
kubectl -n n8n-prod describe pod "$POD" | tail -n 120

# Maintenant seulement, les logs
kubectl -n n8n-prod logs "$POD" --tail=400

kubectl -n n8n-prod delete pod "$POD" --wait=false
