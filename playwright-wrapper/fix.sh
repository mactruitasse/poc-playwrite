#!/usr/bin/env bash
set -euo pipefail

NS="kube-system"
DS="kube-proxy"
NOFILE="${NOFILE:-1048576}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need kubectl
need python3

echo "[i] Reading current kube-proxy args..."
python3 - <<'PY'
import json, subprocess, shlex, sys

NS="kube-system"
DS="kube-proxy"
NOFILE=int(__import__("os").environ.get("NOFILE","1048576"))

raw = subprocess.check_output(["kubectl","-n",NS,"get","ds",DS,"-o","json"])
ds = json.loads(raw)

c = None
for cc in ds["spec"]["template"]["spec"]["containers"]:
    if cc.get("name") == "kube-proxy":
        c = cc
        break
if not c:
    print("ERROR: kube-proxy container not found in ds", file=sys.stderr)
    sys.exit(1)

# Build the original command line as kubernetes would run it.
cmd = c.get("command") or []
args = c.get("args") or []

# If command is empty, entrypoint is image's default; args usually start with "kube-proxy".
orig = cmd + args
if not orig:
    print("ERROR: couldn't determine kube-proxy command/args", file=sys.stderr)
    sys.exit(1)

# Determine what to exec:
# - if first token looks like kube-proxy, exec it as-is
# - else exec "kube-proxy" + args
first = orig[0]
if "kube-proxy" in first:
    exec_tokens = orig
else:
    exec_tokens = ["kube-proxy"] + orig

exec_str = " ".join(shlex.quote(t) for t in exec_tokens)
wrapped = f"ulimit -n {NOFILE} || true; exec {exec_str}"

patch = {
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "kube-proxy",
          "command": ["sh","-c"],
          "args": [wrapped],
        }]
      }
    }
  }
}

print("[i] Applying patch: wrap kube-proxy with ulimit -n", NOFILE)
subprocess.check_call([
  "kubectl","-n",NS,"patch","ds",DS,
  "--type=strategic",
  "-p", json.dumps(patch)
])

print("[OK] Patched daemonset. Now rolling restart...")
PY

kubectl -n "$NS" rollout restart ds/"$DS"
kubectl -n "$NS" rollout status  ds/"$DS" --timeout=180s

echo
echo "[OK] kube-proxy restarted. Current status:"
kubectl -n "$NS" get pods -l k8s-app=kube-proxy -o wide
