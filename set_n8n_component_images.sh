#!/usr/bin/env bash
set -euo pipefail

VALUES_FILE="${1:-apps/n8n/values-prod.yaml}"
TAG="${2:-2.2.4-pw1.49.0-fix4}"
REPO="${3:-n8n-playwright-local}"
PULLPOLICY="${4:-IfNotPresent}"

if [[ ! -f "$VALUES_FILE" ]]; then
  echo "ERROR: file not found: $VALUES_FILE" >&2
  exit 1
fi

BACKUP="${VALUES_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$VALUES_FILE" "$BACKUP"

# Updates/creates: main.image, worker.image, webhook.image
python3 - <<'PY'
import sys, re, pathlib

path = pathlib.Path(sys.argv[1])
tag  = sys.argv[2]
repo = sys.argv[3]
pp   = sys.argv[4]
txt  = path.read_text(encoding="utf-8")

def ensure_section(section: str, txt: str) -> None:
    if not re.search(rf'^{re.escape(section)}:\s*$', txt, flags=re.M):
        raise SystemExit(f"ERROR: top-level section '{section}:' not found (won't guess).")

def upsert_image_block(section: str, txt: str) -> str:
    # Capture the section block: from "section:" to next top-level key or EOF
    m = re.search(rf'^(?P<h>{re.escape(section)}:\s*)\n(?P<body>(?:[ ]{{2}}.*\n)*)', txt, flags=re.M)
    if not m:
        raise SystemExit(f"ERROR: couldn't parse section '{section}:' block.")

    body = m.group("body")

    # If an "  image:" block exists, update its keys. Otherwise, insert it at the top of the section body.
    if re.search(r'^[ ]{2}image:\s*$', body, flags=re.M):
        # Update existing keys if present; if missing, add them right under image:
        lines = body.splitlines(True)
        out = []
        in_image = False
        saw_repo = saw_tag = saw_pp = False
        for i, line in enumerate(lines):
            if re.match(r'^[ ]{2}image:\s*$', line):
                in_image = True
                out.append(line)
                continue
            if in_image:
                # image block ends when indentation returns to 2 spaces with another key, or blank line, or end
                if re.match(r'^[ ]{2}[^ ].*:\s*$', line):
                    # before leaving image block, inject missing keys
                    ins = []
                    if not saw_repo: ins.append(f"    repository: {repo}\n")
                    if not saw_tag:  ins.append(f"    tag: {tag}\n")
                    if not saw_pp:   ins.append(f"    pullPolicy: {pp}\n")
                    out.extend(ins)
                    in_image = False
                    out.append(line)
                    continue

                if re.match(r'^[ ]{4}repository:\s*', line):
                    out.append(f"    repository: {repo}\n"); saw_repo = True; continue
                if re.match(r'^[ ]{4}tag:\s*', line):
                    out.append(f"    tag: {tag}\n"); saw_tag = True; continue
                if re.match(r'^[ ]{4}pullPolicy:\s*', line):
                    out.append(f"    pullPolicy: {pp}\n"); saw_pp = True; continue

            out.append(line)

        # If file ended while still in image block, append missing keys at end
        if in_image:
            if not saw_repo: out.append(f"    repository: {repo}\n")
            if not saw_tag:  out.append(f"    tag: {tag}\n")
            if not saw_pp:   out.append(f"    pullPolicy: {pp}\n")

        new_body = "".join(out)
    else:
        # Insert an image block at the beginning of the section body
        image_block = (
            f"  image:\n"
            f"    repository: {repo}\n"
            f"    tag: {tag}\n"
            f"    pullPolicy: {pp}\n"
        )
        new_body = image_block + body

    # Replace the section body
    start, end = m.start("body"), m.end("body")
    return txt[:start] + new_body + txt[end:]

for sec in ("main", "worker", "webhook"):
    ensure_section(sec, txt)
    txt = upsert_image_block(sec, txt)

path.write_text(txt, encoding="utf-8")
PY "$VALUES_FILE" "$TAG" "$REPO" "$PULLPOLICY"

echo "OK: set main/worker/webhook image to ${REPO}:${TAG} (pullPolicy=${PULLPOLICY}) in ${VALUES_FILE}"
echo "Backup: ${BACKUP}"
