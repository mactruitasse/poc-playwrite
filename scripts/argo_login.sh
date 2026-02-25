#!/usr/bin/env bash
set -euo pipefail

KIND_NODE="${1:-pw-control-plane}"
PORT_HTTP="${PORT_HTTP:-32082}"
PORT_HTTPS="${PORT_HTTPS:-31701}"

NODEIP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$KIND_NODE")"

echo "[+] kind node: $KIND_NODE"
echo "[+] node ip   : $NODEIP"
echo "[+] http port : $PORT_HTTP"
echo "[+] https port: $PORT_HTTPS"
echo

echo "[+] Login via HTTP (insecure)"
argocd login "${NODEIP}:${PORT_HTTP}" --insecure
