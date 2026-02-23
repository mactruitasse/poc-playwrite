#!/usr/bin/env bash
set -euo pipefail

############################################
# CONFIG
############################################
FILE="${FILE:-app/main.py}"
BACKUP_DIR="${BACKUP_DIR:-./_backup_wf_sticky_$(date +%Y%m%d-%H%M%S)}"

############################################
# BACKUP
############################################
mkdir -p "${BACKUP_DIR}"
cp -a "${FILE}" "${BACKUP_DIR}/main.py.bak"
echo "[+] Backup -> ${BACKUP_DIR}/main.py.bak"

############################################
# PATCH (python-based safe edit)
############################################
python3 - <<'PY'
import re, sys, pathlib

p = pathlib.Path("app/main.py")
s = p.read_text(encoding="utf-8")

# 1) Replace global sticky vars
s2 = s
s2 = re.sub(r"STICKY_SESSION_ID:\s*Optional\[str\]\s*=\s*None\s*\nSTICKY_LOCK\s*=\s*asyncio\.Lock\(\)\s*",
            "STICKY_BY_KEY: Dict[str, str] = {}\nSTICKY_LOCK = asyncio.Lock()\n",
            s2)

# 2) Replace _get_or_create_sticky_session_id() with keyed version
pattern = r"async def _get_or_create_sticky_session_id\(\) -> str:\n(?:.|\n)*?\n\n"
m = re.search(pattern, s2)
if not m:
    print("ERROR: cannot find _get_or_create_sticky_session_id() block", file=sys.stderr)
    sys.exit(1)

replacement = """async def _get_or_create_sticky_session_id_for(key: str) -> str:
    async with STICKY_LOCK:
        sid = STICKY_BY_KEY.get(key)
        if sid and sid in SESSIONS:
            return sid
        data = await create_session()
        sid = data.get("sessionId")
        if not sid:
            raise HTTPException(status_code=500, detail="Sticky session creation failed: missing sessionId")
        STICKY_BY_KEY[key] = sid
        return sid


"""
s2 = s2[:m.start()] + replacement + s2[m.end():]

# 3) Update mcp_root / mcp_subpath / sse_root / sse_subpath to use workflowId
def replace_sid(func_name: str, body: str) -> str:
    # expects: sid = await _get_or_create_sticky_session_id()
    body = body.replace("sid = await _get_or_create_sticky_session_id()",
                        "wf = request.query_params.get('workflowId') or 'default'\n    sid = await _get_or_create_sticky_session_id_for(wf)")
    return body

for fn in ["mcp_root", "mcp_subpath", "sse_root", "sse_subpath"]:
    s2_new = re.sub(
        rf"(async def {fn}\(.*?\):\n)([\s\S]*?)(\n\n)",
        lambda m: m.group(1) + replace_sid(fn, m.group(2)) + m.group(3),
        s2,
        count=1,
    )
    s2 = s2_new

# 4) Update delete_session sticky cleanup (remove STICKY_SESSION_ID usage)
s2 = re.sub(r"\n\s*global STICKY_SESSION_ID\n\s*if STICKY_SESSION_ID == session_id:\n\s*STICKY_SESSION_ID = None\n",
            "\n    # Remove from any sticky keys\n    for k, v in list(STICKY_BY_KEY.items()):\n        if v == session_id:\n            STICKY_BY_KEY.pop(k, None)\n",
            s2)

p.write_text(s2, encoding="utf-8")
print("[+] Patched app/main.py")
PY

############################################
# VERIFY
############################################
python3 -m py_compile app/main.py
echo "[+] OK: app/main.py compiles"

echo
echo "[+] Next: rebuild + redeploy wrapper, then in n8n set MCP URL:"
echo "    http://playwright-wrapper.n8n-prod.svc.cluster.local:8080/mcp?workflowId={{\$workflow.id}}"
