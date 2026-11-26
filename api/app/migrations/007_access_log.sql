-- Phase 5.5 : journal d'accès. Qui a fait quoi, quand, avec quel jeton : questions (modèle,
-- chunks et documents lus), connexions, envois de documents, actions d'administration.
--
-- Métadonnées seulement : jamais le texte des questions ni des réponses (les conversations
-- restent privées, admins compris), jamais de mot de passe ni de jeton.
--
-- Journal en ajout seul : l'API peut y écrire, mais ni le modifier ni l'effacer, même par
-- une injection SQL. Il survit à la suppression d'une conversation.

CREATE TABLE access_log (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    user_id  bigint REFERENCES users (id),   -- NULL : connexion échouée, ou app.admin en CLI
    token_id bigint REFERENCES tokens (id),  -- jeton utilisé (session ou clé API)
    action   text NOT NULL CHECK (action IN (
        'ask',              -- question : modèle, collections, chunks et documents lus
        'login',            -- connexion réussie
        'login_failed',     -- connexion refusée (nom tenté)
        'logout',
        'api_key_create',
        'api_key_revoke',
        'document_upload',
        'admin'             -- comptes, collections, groupes, membres, droits
    )),
    client   text,                            -- adresse de l'appelant (X-Forwarded-For)
    detail   jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX access_log_at_idx ON access_log (at DESC);
CREATE INDEX access_log_user_idx ON access_log (user_id, at DESC);
-- « Qui a lu le document 12 ? » : detail @> '{"document_ids": [12]}'
CREATE INDEX access_log_detail_idx ON access_log USING gin (detail jsonb_path_ops);

-- Ni UPDATE ni DELETE pour l'API
GRANT SELECT, INSERT ON access_log TO rag_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rag_api;

ALTER TABLE access_log ENABLE ROW LEVEL SECURITY;
-- Chacun n'écrit que des lignes à son nom ; seule exception, la connexion, qui a lieu avant
-- qu'un jeton existe.
CREATE POLICY api_insert ON access_log FOR INSERT TO rag_api
    WITH CHECK (
        user_id IS NOT DISTINCT FROM (SELECT rag_current_user_id())
        OR action IN ('login', 'login_failed')
    );
-- Lecture réservée aux admins
CREATE POLICY api_admin_read ON access_log FOR SELECT TO rag_api
    USING ((SELECT rag_is_admin()));
CREATE POLICY readonly_all ON access_log FOR SELECT TO rag_readonly USING (true);
