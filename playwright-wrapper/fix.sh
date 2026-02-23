cat > /home/aconte/n8n-gitops/playwright-wrapper/patch_wrapper_ignore_remoteprotocol.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

need_cmd python3
need_cmd date
need_cmd cp

WRAPPER_DIR="${WRAPPER_DIR:-$(pwd)}"
MAIN="${MAIN:-$WRAPPER_DIR/app/main.py}"
[[ -f "$MAIN" ]] || die "main.py not found: $MAIN (set WRAPPER_DIR=/path/to/wrapper)"

ts="$(date +%Y%m%d-%H%M%S)"
bak="$MAIN.bak.ignore-remoteprotocol.$ts"
cp -a "$MAIN" "$bak"
echo "[OK] Backup -> $bak"

python3 - <<PY
from __future__ import annotations
from pathlib import Path
import py_compile
import sys

main_path = Path("$MAIN")
txt = main_path.read_text(encoding="utf-8")
lines = txt.splitlines(True)

MARK = "Upstream SSE closed abruptly"
if MARK in txt:
    print("[OK] Already patched (marker found).")
    py_compile.compile(str(main_path), doraise=True)
    sys.exit(0)

def indent_of(s: str) -> str:
    return s[:len(s) - len(s.lstrip(" "))]

patched = 0
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
            patched += 1
            i = finally_idx + len(ex) + 1
            continue
    i += 1

if patched == 0:
    raise SystemExit("ERROR: No _stream() try/finally blocks found to patch.")

main_path.write_text("".join(lines), encoding="utf-8")
py_compile.compile(str(main_path), doraise=True)
print(f"[OK] Patched main.py: streams_patched={patched}")
PY

echo "[OK] Done."
EOF

chmod +x /home/aconte/n8n-gitops/playwright-wrapper/patch_wrapper_ignore_remoteprotocol.sh
