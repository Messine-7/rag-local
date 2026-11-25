-- Phase 5.3 : conversations et messages (POST /ask). Chacun ne voit que ses conversations,
-- admins compris : c'est Postgres qui filtre (Row-Level Security), comme pour les documents.

CREATE TABLE conversations (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title      text NOT NULL,  -- début de la première question
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_user_idx ON conversations (user_id, updated_at DESC);

CREATE TABLE messages (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id   bigint NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role              text NOT NULL CHECK (role IN ('user', 'assistant')),
    content           text NOT NULL,
    -- Réponses seulement : modèle, passages cités (chunk, document, page, score), mesures
    model             text,
    citations         jsonb,
    prompt_tokens     integer,
    completion_tokens integer,
    latency_ms        integer,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, id);

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rag_api;
GRANT SELECT, INSERT, UPDATE (title, updated_at), DELETE ON conversations TO rag_api;
GRANT SELECT, INSERT ON messages TO rag_api;

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_own ON conversations FOR ALL TO rag_api
    USING (user_id = (SELECT rag_current_user_id()))
    WITH CHECK (user_id = (SELECT rag_current_user_id()));

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
-- La sous-requête sur conversations est elle-même filtrée : seulement ses conversations
CREATE POLICY api_own ON messages FOR ALL TO rag_api
    USING (conversation_id IN (SELECT id FROM conversations))
    WITH CHECK (conversation_id IN (SELECT id FROM conversations));

CREATE POLICY readonly_all ON conversations FOR SELECT TO rag_readonly USING (true);
CREATE POLICY readonly_all ON messages FOR SELECT TO rag_readonly USING (true);
