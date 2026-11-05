#!/usr/bin/env bash
# docker compose sur le PC fixe, avec la configuration de production et la version déployée.
# Exemples :
#   bash deploy/compose.sh ps
#   bash deploy/compose.sh logs -f ollama
#   bash deploy/compose.sh exec ollama ollama pull bge-m3
#   bash deploy/compose.sh up -d ollama api      (après avoir modifié .env)
set -euo pipefail

deploy_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$deploy_dir")"

owner_re='github\.com[/:]([^/]+)/'
remote_url="$(git remote get-url origin)"
if [[ ! "$remote_url" =~ $owner_re ]]; then
  echo "remote origin inattendu : $remote_url" >&2
  exit 1
fi
export GHCR_OWNER="${BASH_REMATCH[1],,}"

if [[ ! -s "$deploy_dir/.last-deployed" ]]; then
  echo "aucune version déployée (deploy/.last-deployed absent) : lance d'abord pull-deploy.sh" >&2
  exit 1
fi
APP_VERSION="$(tr -d '[:space:]' <"$deploy_dir/.last-deployed")"
export APP_VERSION

exec docker compose -f docker-compose.yml -f deploy/compose.prod.yml "$@"
