IMG="n8n-playwright-local:2.2.4-pw1.49.0-fix1"
POD="n8n-debug-$(date +%s)"

kubectl -n n8n-prod run "$POD" --restart=Never --image="$IMG" --command -- sh -lc 'sleep 3600'   # garde le pod vivant

kubectl -n n8n-prod wait --for=condition=Ready "pod/$POD" --timeout=120s

# Une fois Ready, inspection des modules n8n
kubectl -n n8n-prod exec -it "$POD" -- sh -lc '
set -eux
node -v || true
MOD="/usr/lib/node_modules/n8n/dist/modules"
[ -d /usr/local/lib/node_modules/n8n/dist/modules ] && MOD="/usr/local/lib/node_modules/n8n/dist/modules"
echo "MOD=$MOD"
ls -la "$MOD" | head -n 200
ls -la "$MOD"/community-packages* || true
ls -la "$MOD"/community-packages/community-packages.module* || true
ls -la "$MOD"/community-packages.ee || true
ls -la "$MOD"/community-packages.ee/community-packages.module* || true
'

# Nettoyage
kubectl -n n8n-prod delete pod "$POD" --wait=false
