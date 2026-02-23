#!/usr/bin/env bash
set -euo pipefail

# Fix ArgoCD OutOfSync caused by Helm-generated secrets drifting from live cluster values:
# - n8n encryption key: switch to existing secret
# - postgresql: switch to existing secret (so Helm stops generating new passwords)
#
# Repo layout assumed (from your tree):
#   ~/n8n-gitops/apps/n8n/values-prod.yaml

ROOT="${ROOT:-$HOME/n8n-gitops}"
VALUES="$ROOT/apps/n8n/values-prod.yaml"
NS="${NS:-n8n-prod}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing command: $1" >&2; exit 1; }; }
need kubectl
need python3
need base64
need helm

if [[ ! -f "$VALUES" ]]; then
  echo "ERROR: values file not found: $VALUES" >&2
  exit 1
fi

ts="$(date +%Y%m%d-%H%M%S)"
bak="${VALUES}.bak.${ts}"
cp -a "$VALUES" "$bak"
echo "[+] Backup: $bak"

echo "[+] Reading live secrets from cluster (namespace=$NS)"
live_n8n_key="$(kubectl -n "$NS" get secret n8n-prod-encryption-key-secret-v2 -o jsonpath='{.data.N8N_ENCRYPTION_KEY}' | base64 -d)"
live_pg_postgres_pw="$(kubectl -n "$NS" get secret n8n-prod-postgresql -o jsonpath='{.data.postgres-password}' | base64 -d)"
live_pg_pw="$(kubectl -n "$NS" get secret n8n-prod-postgresql -o jsonpath='{.data.password}' | base64 -d)"

echo "[+] Live values:"
echo "    N8N_ENCRYPTION_KEY: $live_n8n_key"
echo "    Postgres postgres-password: $live_pg_postgres_pw"
echo "    Postgres password: $live_pg_pw"

echo "[+] Patching values-prod.yaml (set existing secrets so Helm stops regenerating)"
python3 - "$VALUES" <<'PY'
import sys, re

path = sys.argv[1]
txt = open(path, "r", encoding="utf-8").read()

def ensure_top_level_key(txt: str, key: str, value: str) -> str:
    # Ensure a top-level YAML key exists with exact "key: value" line.
    # Replace if present; otherwise append at end with a newline.
    pat = re.compile(rf"(?m)^(?P<indent>[ ]*){re.escape(key)}\s*:\s*.*$")
    repl = f"{key}: {value}"
    if pat.search(txt):
        txt = pat.sub(repl, txt, count=1)
    else:
        if not txt.endswith("\n"):
            txt += "\n"
        txt += f"\n{repl}\n"
    return txt

def ensure_block(txt: str, parent: str, child_lines: list[str]) -> str:
    # Ensure a parent block exists, and ensure/replace children (2 spaces indentation).
    # If parent doesn't exist, append it at end.
    # If child key exists under parent, replace its value line.
    # Otherwise, insert children after parent line.
    parent_pat = re.compile(rf"(?m)^(?P<indent>[ ]*){re.escape(parent)}\s*:\s*$")
    m = parent_pat.search(txt)
    if not m:
        if not txt.endswith("\n"):
            txt += "\n"
        txt += f"\n{parent}:\n"
        for line in child_lines:
            txt += f"  {line}\n"
        return txt

    # Find parent block span (until next top-level key)
    start = m.end()
    # next top-level key: begins at column 0 with "word:"
    next_top = re.search(r"(?m)^[A-Za-z0-9_.-]+\s*:\s*$", txt[start:])
    end = start + (next_top.start() if next_top else len(txt) - start)
    block = txt[start:end]

    # For each child line like "auth:" or "existingSecret: foo" we handle leaf keys only.
    for cl in child_lines:
        # Only leaf "k: v" supported for this helper
        if ":" not in cl:
            continue
        k = cl.split(":", 1)[0].strip()
        leaf_pat = re.compile(rf"(?m)^[ ]{{2}}{re.escape(k)}\s*:\s*.*$")
        if leaf_pat.search(block):
            block = leaf_pat.sub(f"  {cl}", block, count=1)
        else:
            # insert at end of block (keep one trailing newline)
            if not block.endswith("\n"):
                block += "\n"
            block += f"  {cl}\n"

    return txt[:start] + block + txt[end:]

# 1) n8n encryption key: use existing secret
txt = ensure_top_level_key(txt, "existingEncryptionKeySecret", "n8n-prod-encryption-key-secret-v2")

# 2) postgresql: use existing secret, and keep any other auth settings intact
# Ensure:
# postgresql:
#   auth:
#     existingSecret: n8n-prod-postgresql
#
# We'll add postgresql.auth block if missing.

# Ensure postgresql: exists
if not re.search(r"(?m)^postgresql\s*:\s*$", txt):
    if not txt.endswith("\n"):
        txt += "\n"
    txt += "\npostgresql:\n"

# Ensure auth: exists under postgresql:
# If postgresql block has auth, we set existingSecret. Else we add auth with existingSecret.
# Find postgresql block
m = re.search(r"(?m)^postgresql\s*:\s*$", txt)
start = m.end()
next_top = re.search(r"(?m)^[A-Za-z0-9_.-]+\s*:\s*$", txt[start:])
end = start + (next_top.start() if next_top else len(txt) - start)
pg_block = txt[start:end]

if re.search(r"(?m)^[ ]{2}auth\s*:\s*$", pg_block):
    # inside auth block: set existingSecret
    # find auth block boundaries within pg_block
    am = re.search(r"(?m)^[ ]{2}auth\s*:\s*$", pg_block)
    astart = am.end()
    # next key at 2 spaces indent within postgresql
    next_2 = re.search(r"(?m)^[ ]{2}[A-Za-z0-9_.-]+\s*:\s*$", pg_block[astart:])
    aend = astart + (next_2.start() if next_2 else len(pg_block) - astart)
    auth_block = pg_block[astart:aend]

    if re.search(r"(?m)^[ ]{4}existingSecret\s*:\s*.*$", auth_block):
        auth_block = re.sub(r"(?m)^[ ]{4}existingSecret\s*:\s*.*$",
                            "    existingSecret: n8n-prod-postgresql",
                            auth_block, count=1)
    else:
        if not auth_block.endswith("\n"):
            auth_block += "\n"
        auth_block += "    existingSecret: n8n-prod-postgresql\n"

    pg_block = pg_block[:astart] + auth_block + pg_block[aend:]
else:
    # add auth block at end of postgresql block
    if not pg_block.endswith("\n"):
        pg_block += "\n"
    pg_block += "  auth:\n    existingSecret: n8n-prod-postgresql\n"

txt = txt[:start] + pg_block + txt[end:]

open(path, "w", encoding="utf-8").write(txt)
PY

echo "[+] Quick diff (values-prod.yaml vs backup):"
# show only relevant lines
grep -nE '^(existingEncryptionKeySecret:|postgresql:|  auth:|    existingSecret:)' "$VALUES" || true

echo
echo "[+] Render Helm secrets (desired) and compare with live (decoded)"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# Render both secrets from Helm output (if present)
helm template n8n-prod "$ROOT/charts/n8n" -n "$NS" -f "$VALUES" > "$tmpdir/render.yaml"

# Extract desired base64 values if present
desired_n8n_b64="$(awk 'BEGIN{RS="---"} /kind: Secret/ && /name: n8n-prod-encryption-key-secret-v2/ {print}' "$tmpdir/render.yaml" \
  | awk '/N8N_ENCRYPTION_KEY:/ {print $2}' | tr -d '"' || true)"

desired_pg_postgres_b64="$(awk 'BEGIN{RS="---"} /kind: Secret/ && /name: n8n-prod-postgresql/ {print}' "$tmpdir/render.yaml" \
  | awk '/postgres-password:/ {print $2}' | tr -d '"' || true)"

desired_pg_pw_b64="$(awk 'BEGIN{RS="---"} /kind: Secret/ && /name: n8n-prod-postgresql/ {print}' "$tmpdir/render.yaml" \
  | awk '/^[ ]+password:/ {print $2}' | tr -d '"' || true)"

decode_b64() { [[ -n "${1:-}" ]] && printf '%s' "$1" | base64 -d 2>/dev/null || true; }

desired_n8n_key="$(decode_b64 "$desired_n8n_b64")"
desired_pg_postgres_pw="$(decode_b64 "$desired_pg_postgres_b64")"
desired_pg_pw="$(decode_b64 "$desired_pg_pw_b64")"

echo "    desired N8N_ENCRYPTION_KEY: ${desired_n8n_key:-<not rendered>}"
echo "    desired Postgres postgres-password: ${desired_pg_postgres_pw:-<not rendered>}"
echo "    desired Postgres password: ${desired_pg_pw:-<not rendered>}"

echo
echo "[+] Comparisons:"
if [[ -z "$desired_n8n_key" ]]; then
  echo "    N8N_ENCRYPTION_KEY: not rendered by Helm (OK if chart uses existing secret instead of managing data)"
else
  [[ "$desired_n8n_key" == "$live_n8n_key" ]] \
    && echo "    N8N_ENCRYPTION_KEY: MATCH" \
    || echo "    N8N_ENCRYPTION_KEY: MISMATCH (live=$live_n8n_key desired=$desired_n8n_key)"
fi

if [[ -z "$desired_pg_postgres_pw" ]]; then
  echo "    postgres-password: not rendered by Helm (OK if existingSecret makes chart stop templating the secret)"
else
  [[ "$desired_pg_postgres_pw" == "$live_pg_postgres_pw" ]] \
    && echo "    postgres-password: MATCH" \
    || echo "    postgres-password: MISMATCH (live=$live_pg_postgres_pw desired=$desired_pg_postgres_pw)"
fi

if [[ -n "$desired_pg_pw" ]]; then
  [[ "$desired_pg_pw" == "$live_pg_pw" ]] \
    && echo "    password: MATCH" \
    || echo "    password: MISMATCH (live=$live_pg_pw desired=$desired_pg_pw)"
else
  echo "    password: not rendered (OK if secret not templated due to existingSecret)"
fi

echo
echo "[+] Next (manual): git add/commit/push then Argo sync (or wait if automated)."
echo "    values file: $VALUES"
echo "    backup:      $bak"
