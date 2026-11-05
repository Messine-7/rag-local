#!/usr/bin/env bash
# Déploiement « pull » du PC fixe (Docker dans WSL).
#
# Rien n'est envoyé depuis GitHub vers cette machine : c'est elle qui vient chercher
# le dernier commit de main et l'image Docker que la CI a construite et testée pour ce commit.
# Tant que l'image n'existe pas (CI en cours ou en échec), rien ne change.
# Si la nouvelle version ne démarre pas, la version précédente est relancée.
# Après un déploiement réussi, seules l'image en service et la précédente sont gardées.
# Lancé toutes les 2 minutes par un timer systemd (voir install-timer.sh).

# Pas de "set -e" : chaque code de retour est contrôlé à la main.
set -uo pipefail

deploy_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$deploy_dir")"
cd "$repo" || exit 1

compose=(docker compose -f docker-compose.yml -f deploy/compose.prod.yml)
state_file="$deploy_dir/.last-deployed"
failed_file="$deploy_dir/.last-failed"
log_file="$deploy_dir/deploy.log"

# Journal : deploy.log, et la sortie standard (visible avec journalctl)
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$log_file"; }
log_output() { sed 's/^/    /' | tee -a "$log_file"; }

read_state() { if [[ -f "$1" ]]; then tr -d '[:space:]' <"$1"; fi; }

# Lance une version et attend que l'API soit saine ; code de retour 0 si c'est le cas
start_version() {
  export APP_VERSION="$1"
  "${compose[@]}" up -d --no-build --wait --wait-timeout 120 2>&1 | log_output
  return "${PIPESTATUS[0]}"
}

# Supprime les images de l'API, sauf les tags passés en arguments (en service et précédente)
remove_old_images() {
  local name="$1" tag
  shift
  for tag in $(docker image ls "$name" --format '{{.Tag}}'); do
    [[ " $* " == *" $tag "* ]] || docker image rm "$name:$tag" >/dev/null 2>&1
  done
  docker image prune -f >/dev/null 2>&1 # couches orphelines
}

# Propriétaire GitHub, déduit du remote (ghcr.io impose des minuscules)
remote_url="$(git remote get-url origin)"
owner_re='github\.com[/:]([^/]+)/'
if [[ ! "$remote_url" =~ $owner_re ]]; then
  log "remote origin inattendu : $remote_url"
  exit 1
fi
owner="${BASH_REMATCH[1],,}"
image_name="ghcr.io/$owner/rag-local-api"
export GHCR_OWNER="$owner"

if ! git fetch --quiet origin main; then
  log "git fetch a échoué"
  exit 1
fi
target="$(git rev-parse origin/main)"
current="$(read_state "$state_file")"

# Déjà déployé, ou déjà tenté sans succès : on attend le commit suivant
if [[ "$target" == "$current" || "$target" == "$(read_state "$failed_file")" ]]; then
  exit 0
fi

# L'image n'est publiée que si la CI est passée pour ce commit
if ! docker pull --quiet "$image_name:$target" >/dev/null 2>&1; then
  exit 0 # pas encore publiée : on réessaiera au prochain passage
fi

if ! git merge --ff-only --quiet origin/main; then
  log "fast-forward impossible (modifications locales ?)"
  exit 1
fi

log "déploiement de $target"
if start_version "$target"; then
  echo "$target" >"$state_file"
  log "déployé $target"
  remove_old_images "$image_name" "$target" "$current"
  exit 0
fi

echo "$target" >"$failed_file"
log "ÉCHEC du déploiement de $target, dernières lignes de l'API :"
"${compose[@]}" logs --tail 50 api 2>&1 | log_output

# Retour arrière : on relance l'image précédente (le clone, lui, reste sur le nouveau commit)
if [[ -z "$current" ]]; then
  log "aucune version précédente : l'API reste hors service"
  exit 1
fi
log "retour à la version précédente $current"
if start_version "$current"; then
  log "retour arrière réussi : $current est en service"
else
  log "ÉCHEC du retour arrière : l'API est hors service"
fi
exit 1
