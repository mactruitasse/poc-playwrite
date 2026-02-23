#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-n8n-prod}"
LABEL_SEL="${LABEL_SEL:-app.kubernetes.io/name=playwright-wrapper,app.kubernetes.io/instance=n8n-prod}"
OUT_DIR="${OUT_DIR:-./dump_wrapper_code_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$OUT_DIR"

echo "[+] OUT_DIR=$OUT_DIR"
echo "[+] NS=$NS"
echo "[+] LABEL_SEL=$LABEL_SEL"
echo

POD="$(kubectl -n "$NS" get pod -l "$LABEL_SEL" -o jsonpath='{.items[0].metadata.name}')"
echo "[+] Wrapper pod: $POD" | tee "$OUT_DIR/pod.txt"

echo
echo "[+] Image"
kubectl -n "$NS" get pod "$POD" -o jsonpath='{.spec.containers[0].image}{"\n"}' | tee "$OUT_DIR/image.txt"

echo
echo "[+] openapi.json (for proof)"
kubectl -n "$NS" exec "$POD" -- sh -lc 'python3 - << "PY"
from app.main import app
import json
print(json.dumps(app.openapi(), indent=2)[:4000])
PY' | tee "$OUT_DIR/openapi_excerpt.json" >/dev/null || true

echo
echo "[+] Dump /app/app/main.py + grep routes"
kubectl -n "$NS" exec "$POD" -- sh -lc '
set -e
echo "== ls -la /app/app ==";
ls -la /app/app;
echo;
echo "== sha256(main.py) ==";
python3 - << "PY"
import hashlib
p="/app/app/main.py"
h=hashlib.sha256(open(p,"rb").read()).hexdigest()
print(h, p)
PY
echo;
echo "== grep decorators ==";
grep -nE "^[[:space:]]*@app\\.(get|post|delete|api_route)" /app/app/main.py || true;
echo;
echo "== grep sessions/mcp keywords ==";
grep -nE "(/sessions|create_session|delete_session|mcp_root|proxy_any)" /app/app/main.py || true;
echo;
echo "== main.py (first 260 lines) ==";
nl -ba /app/app/main.py | sed -n "1,260p";
echo;
echo "== main.py (last 120 lines) ==";
nl -ba /app/app/main.py | tail -n 120;
' | tee "$OUT_DIR/main_py_dump.txt"

echo
echo "[DONE] $OUT_DIR"
echo "Key file: $OUT_DIR/main_py_dump.txt"
