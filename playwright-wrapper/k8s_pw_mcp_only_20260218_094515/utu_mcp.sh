#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-n8n-prod}"
PW_SVC="${PW_SVC:-pw-cfa20bbf48b4d847}"
PW_PORT="${PW_PORT:-8933}"
CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.6.0}"
MAX_TIME="${MAX_TIME:-15}"

kubectl -n "${NS}" get svc "${PW_SVC}" >/dev/null

kubectl -n "${NS}" run "curlmcp-$(date +%s)" --rm -i --restart=Never --image="${CURL_IMAGE}" -- \
  sh -lc "
    set -eu
    base='http://${PW_SVC}:${PW_PORT}'

    echo '--- A) GET /mcp (juste pour voir le code) ---'
    curl -sv --max-time ${MAX_TIME} \"\$base/mcp\" 2>&1 | tail -n 120 || true

    echo
    echo '--- B) POST /mcp JSON-RPC __ping__ ---'
    curl -sv --max-time ${MAX_TIME} \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json' \
      -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"__ping__\",\"params\":{}}' \
      \"\$base/mcp\" 2>&1 | tail -n 200 || true
  "
