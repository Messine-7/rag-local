#!/usr/bin/env bash
# Installe le service et le timer systemd qui lancent pull-deploy.sh toutes les 2 minutes.
# À lancer une seule fois sur le PC fixe, dans WSL, avec son compte habituel :
#   bash deploy/install-timer.sh
# (sudo est demandé pour écrire dans /etc/systemd/system)
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
user="$(id -un)"

if [[ "$(ps -p 1 -o comm=)" != "systemd" ]]; then
  echo "systemd n'est pas actif dans WSL." >&2
  echo "Ajoute dans /etc/wsl.conf :  [boot]  puis  systemd=true" >&2
  echo "puis lance « wsl --shutdown » dans PowerShell et rouvre WSL." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker ne répond pas pour l'utilisateur $user." >&2
  echo "Vérifie que Docker tourne et que $user est dans le groupe docker :" >&2
  echo "  sudo usermod -aG docker $user   (puis ferme et rouvre WSL)" >&2
  exit 1
fi

sudo tee /etc/systemd/system/rag-local-deploy.service >/dev/null <<EOF
[Unit]
Description=Déploiement pull du projet RAG local
Wants=docker.service network-online.target
After=docker.service network-online.target

[Service]
Type=oneshot
User=$user
WorkingDirectory=$repo
ExecStart=/bin/bash $repo/deploy/pull-deploy.sh
TimeoutStartSec=10min
EOF

sudo tee /etc/systemd/system/rag-local-deploy.timer >/dev/null <<EOF
[Unit]
Description=Lance le déploiement pull du RAG local toutes les 2 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now rag-local-deploy.timer

echo "Timer installé. État : systemctl list-timers rag-local-deploy.timer"
echo "Journal : $repo/deploy/deploy.log  (ou journalctl -u rag-local-deploy)"
