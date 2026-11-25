-- Phase 4 : documents, chunks vectorisés et file de jobs d'ingestion.
-- Une migration appliquée ne se modifie plus : tout changement passe par un nouveau fichier
-- (002_…sql). Voir app/db.py.

CREATE EXTENSION IF NOT EXISTS vector;

-- Collections de documents. Une seule pour l'instant ; les droits groupe → collection
-- arrivent en phase 5 (gouvernance), d'où le collection_id dès maintenant.
CREATE TABLE collections (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO collections (name) VALUES ('défaut');

CREATE TABLE documents (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_id bigint NOT NULL REFERENCES collections (id),
    filename      text NOT NULL,
    sha256        text NOT NULL, -- le fichier est rangé sous <UPLOAD_DIR>/<sha256>.pdf
    size_bytes    bigint NOT NULL,
    pages         integer,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (collection_id, sha256) -- le même PDF n'est pas indexé deux fois dans une collection
);

CREATE TABLE chunks (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id   bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    collection_id bigint NOT NULL REFERENCES collections (id), -- recopié : filtre direct (RLS en phase 5)
    chunk_index   integer NOT NULL,
    page          integer NOT NULL, -- page de départ, numérotée à partir de 1 (citations)
    content       text NOT NULL,
    -- Plein texte pour la recherche hybride (phase 5), calculé par Postgres
    content_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('french', content)) STORED,
    -- 1024 = dimension de bge-m3. Changer de modèle d'embeddings = nouvelle migration + réindexation.
    embedding     vector(1024) NOT NULL,
    embed_model   text NOT NULL, -- modèle qui a produit le vecteur
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_content_tsv_idx ON chunks USING gin (content_tsv);
CREATE INDEX chunks_collection_idx ON chunks (collection_id);

-- File de jobs : lue par le worker avec SELECT … FOR UPDATE SKIP LOCKED (app/worker.py).
CREATE TABLE jobs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts    integer NOT NULL DEFAULT 0,
    run_after   timestamptz NOT NULL DEFAULT now(), -- reporté après un échec temporaire
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz
);
CREATE INDEX jobs_pending_idx ON jobs (run_after, id) WHERE status = 'pending';
CREATE INDEX jobs_document_idx ON jobs (document_id);
