#!/usr/bin/env bash
set -euo pipefail

# Update Helm/YAML values in this repo so the playwright-wrapper Deployment uses a new local kind image tag.
#
# Usage:
#   ./update_playwright_wrapper_image.sh 1.0.1-fix-host
#
# Optional:
#   WRAPPER_REPO=playwright-wrapper ./update_playwright_wrapper_image.sh 1.0.1-fix-host

NEW_TAG="${1:-}"
if [[ -z "${NEW_TAG}" ]]; then
  echo "Usage: $0 <new_tag>"
  echo "Example: $0 1.0.1-fix-host"
  exit 1
fi

WRAPPER_REPO="${WRAPPER_REPO:-playwright-wrapper}"
timestamp="$(date +%Y%m%d%H%M%S)"

backup_if_exists() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  cp -a "$f" "${f}.bak.${timestamp}"
}

# Back up likely targets (best effort)
CANDIDATES=(
  "charts/n8n/charts/playwright-wrapper/values.yaml"
  "apps/n8n/values-prod.yaml"
  "charts/apps/n8n/values-prod.yaml"
  "charts/n8n/values.yaml"
)

for f in "${CANDIDATES[@]}"; do
  backup_if_exists "$f"
done

# Back up any values*.yaml that mention playwright-wrapper (best effort)
while IFS= read -r -d '' f; do
  backup_if_exists "$f"
done < <(find . -type f \( -name 'values*.yaml' -o -name 'values*.yml' \) -print0 \
        | xargs -0 grep -l "playwright-wrapper" -Z 2>/dev/null || true)

export NEW_TAG WRAPPER_REPO

python3 - <<'PY'
import os
from pathlib import Path

import yaml  # PyYAML

NEW_TAG = os.environ["NEW_TAG"]
WRAPPER_REPO = os.environ["WRAPPER_REPO"]

def load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] YAML parse failed: {path} ({e})")
        return None

def dump_yaml(path: Path, data):
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

def ensure_image_block(d: dict):
    img = d.get("image")
    if not isinstance(img, dict):
        img = {}
        d["image"] = img
    img["repository"] = WRAPPER_REPO
    img["tag"] = NEW_TAG

def replace_playwright_wrapper_tag_in_string(s: str) -> str:
    # Replace occurrences like "playwright-wrapper:anything" -> "playwright-wrapper:<NEW_TAG>"
    token = "playwright-wrapper:"
    idx = s.find(token)
    if idx == -1:
        return s

    out = s
    start = 0
    while True:
        i = out.find(token, start)
        if i == -1:
            break
        j = i + len(token)
        # tag token ends at first whitespace, quote, newline, comma, or end of string
        end = j
        while end < len(out) and out[end] not in " \t\r\n'\",)":
            end += 1
        out = out[:j] + NEW_TAG + out[end:]
        start = j + len(NEW_TAG)
    return out

def walk(obj):
    if isinstance(obj, dict):
        # namespaced values: playwright-wrapper:
        if "playwright-wrapper" in obj and isinstance(obj["playwright-wrapper"], dict):
            ensure_image_block(obj["playwright-wrapper"])

        # direct image block that points to wrapper
        img = obj.get("image")
        if isinstance(img, dict):
            repo = img.get("repository")
            if isinstance(repo, str) and "playwright-wrapper" in repo:
                img["repository"] = WRAPPER_REPO
                img["tag"] = NEW_TAG

        for k in list(obj.keys()):
            obj[k] = walk(obj[k])
        return obj

    if isinstance(obj, list):
        return [walk(v) for v in obj]

    if isinstance(obj, str):
        return replace_playwright_wrapper_tag_in_string(obj)

    return obj

# Collect files to update:
files = set()

# Known important files (if present)
for p in [
    Path("charts/n8n/charts/playwright-wrapper/values.yaml"),
    Path("apps/n8n/values-prod.yaml"),
    Path("charts/apps/n8n/values-prod.yaml"),
    Path("charts/n8n/values.yaml"),
]:
    if p.exists():
        files.add(p)

# Any values*.yaml/yml containing the string "playwright-wrapper"
for p in Path(".").rglob("values*.yaml"):
    try:
        if "playwright-wrapper" in p.read_text(encoding="utf-8"):
            files.add(p)
    except Exception:
        pass
for p in Path(".").rglob("values*.yml"):
    try:
        if "playwright-wrapper" in p.read_text(encoding="utf-8"):
            files.add(p)
    except Exception:
        pass

changed = []
for p in sorted(files):
    data = load_yaml(p)
    if data is None:
        continue
    if not isinstance(data, (dict, list)):
        # skip weird YAML roots
        continue

    before = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    data2 = walk(data)
    after = yaml.safe_dump(data2, sort_keys=False, default_flow_style=False)

    if after != before:
        dump_yaml(p, data2)
        changed.append(str(p))

# Ensure subchart values always has image.repository/tag set even if it didn't contain it before
p_sub = Path("charts/n8n/charts/playwright-wrapper/values.yaml")
if p_sub.exists():
    data = load_yaml(p_sub)
    if not isinstance(data, dict):
        data = {}
    ensure_image_block(data)
    data = walk(data)
    dump_yaml(p_sub, data)
    if str(p_sub) not in changed:
        changed.append(str(p_sub))

print("Updated:")
print("  repository =", WRAPPER_REPO)
print("  tag        =", NEW_TAG)
print("Files changed:")
for f in changed:
    print(" -", f)
PY

echo
echo "Done. Backups suffix: .bak.${timestamp}"
echo "Next:"
echo "  kind load docker-image ${WRAPPER_REPO}:${NEW_TAG}"
echo "  # then: helm upgrade / ArgoCD sync"
