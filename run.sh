#!/usr/bin/env bash
set -euo pipefail

############################################
# Variables configurables
############################################
TARGET_SCRIPT="${TARGET_SCRIPT:-./script.sh}"
BACKUP_DIR="${BACKUP_DIR:-./backup_script_fix}"
TS="$(date +%Y%m%d-%H%M%S)"

############################################
# Helpers
############################################
die() { echo "ERREUR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "commande requise introuvable: $1"; }

need mkdir
need cp
need sed
need grep
need nl
need bash

[[ -f "$TARGET_SCRIPT" ]] || die "fichier introuvable: $TARGET_SCRIPT"

############################################
# Sauvegarde
############################################
mkdir -p "$BACKUP_DIR"
BACKUP_PATH="${BACKUP_DIR}/$(basename "$TARGET_SCRIPT").${TS}.bak"
cp -a "$TARGET_SCRIPT" "$BACKUP_PATH"
echo "✅ Sauvegarde créée: $BACKUP_PATH"

############################################
# Correction
# Ajoute un espace avant # quand il est collé à la fin d'une affectation
# Exemple: VAR="x"# comment  -> VAR="x" # comment
############################################
TMP="${TARGET_SCRIPT}.tmp.$$"

# Remplace }"# par }" #
# (couvre exactement ton cas SSE_READ_MAX_TIME=...}"# ...)
sed 's/}"#/}" #/g' "$TARGET_SCRIPT" > "$TMP"
mv -f "$TMP" "$TARGET_SCRIPT"

############################################
# Vérification
############################################
echo
echo "==> Lignes avec commentaire collé (doit être vide)"
# Cherche les cas les plus courants : "..."}"# ou "...'"# ou "...)"#
grep -nE '}"#|'\''#|\)#' "$TARGET_SCRIPT" || true

echo
echo "==> Re-test syntaxe"
bash -n "$TARGET_SCRIPT"
echo "✅ bash -n: OK"

echo
echo "==> Extrait autour de l'ancienne ligne 15 (10-20)"
nl -ba "$TARGET_SCRIPT" | sed -n '10,20p'
