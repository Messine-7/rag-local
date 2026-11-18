# Construite depuis la racine du repo (pyproject.toml et uv.lock y sont) :
#   docker build -f api/Dockerfile .
# Fichiers envoyés au build : voir api/Dockerfile.dockerignore.
FROM python:3.12-slim

# uv, à la version fixée dans pyproject.toml (tool.uv.required-version)
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dépendances d'abord : cette couche n'est reconstruite que si pyproject.toml ou uv.lock changent.
# --frozen : installe exactement uv.lock, sans le recalculer ; --no-dev : ni pytest ni ruff.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY api/app ./app

EXPOSE 8000

# 0.0.0.0 à l'intérieur du conteneur ; la restriction à localhost se fait dans docker-compose.yml
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
