# RAG local

![CI/CD](https://github.com/messine-7/rag-local/actions/workflows/ci-cd.yml/badge.svg)

Assistant de questions-réponses sur des documents PDF et une base PostgreSQL, entièrement
auto-hébergé : modèles, base vectorielle et API tournent sur une machine personnelle,
interrogée à distance via un réseau privé.

> Projet en construction, phase par phase. Phase actuelle : socle API + CI/CD.

## Architecture

```mermaid
flowchart LR
  L["Laptop<br/>dev + client"] -- git push --> G["GitHub<br/>CI + ghcr.io"]
  P -- "pull : commit + image testée" --> G
  L -- "Tailscale (HTTPS)" --> A
  subgraph P["PC fixe — Docker Compose dans WSL"]
    A["API FastAPI"]
  end
```

| Brique | Choix | Pourquoi |
|---|---|---|
| API | FastAPI (async) | plusieurs questions servies en parallèle |
| Réseau | Tailscale + `tailscale serve` | aucun port ouvert sur la box, HTTPS automatique |
| CI | GitHub Actions | lint (ruff), puis une seule image construite, testée (pytest, démarrage) et publiée |
| CD | déploiement « pull » | le repo est public : aucun code venu de GitHub ne s'exécute sur la machine hôte, elle récupère seulement l'image validée par la CI, et revient seule à la version précédente si la nouvelle ne démarre pas |

## Lancer en local

Prérequis : Docker.

```bash
git clone https://github.com/messine-7/rag-local.git
cd rag-local
docker compose up -d --build --wait
curl http://localhost:8000/health
```

## Développer

```bash
python -m venv .venv
# Windows : .venv\Scripts\activate   —   Linux : source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest
```

## Déploiement

1. Chaque push sur `main` lance la CI sur les runners GitHub : lint, build de l'image une
   seule fois, tests pytest dans un conteneur créé à partir de cette image, puis test de
   démarrage (`/health` doit répondre).
2. Si tout passe, cette même image est publiée sur `ghcr.io/messine-7/rag-local-api`, taguée avec le commit.
3. Sur la machine hôte (Windows + Docker dans WSL), un timer systemd lance `deploy/pull-deploy.sh`
   toutes les 2 minutes. Dès que l'image du dernier commit existe, le script met à jour le clone
   et relance les conteneurs ; sinon il ne touche à rien.
4. Si la nouvelle version ne démarre pas, le script relance la précédente. Il ne garde que
   l'image en service et la précédente, pour libérer le disque.

`GET /health` renvoie le commit déployé dans le champ `version`.

## Structure

| Dossier | Rôle |
|---|---|
| `api/` | API FastAPI |
| `worker/` | ingestion PDF (à venir) |
| `client/` | client en ligne de commande (à venir) |
| `tests/` | tests pytest |
| `deploy/` | déploiement pull sur la machine hôte (`install-timer.sh` l'installe) |
| `.github/workflows/` | CI et publication de l'image |
