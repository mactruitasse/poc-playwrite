kubectl -n argocd get applications.argoproj.io
kubectl -n argocd get application "$APP" -o jsonpath='{.status.sync.status}{" | "}{.status.health.status}{" | op="}{.status.operationState.phase}{"\n"}'
