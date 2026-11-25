-- Phase 5.2 : gouvernance des données. Groupes d'utilisateurs, droits groupe → collection
-- (read : lire et interroger ; write : en plus, envoyer des PDF), appliqués par Postgres
-- lui-même (Row-Level Security), pas seulement par le code de l'API.
--
-- Rôles :
--   rag          propriétaire (migrations, worker, app.admin) : la RLS ne s'applique pas à lui ;
--   rag_api      l'API (connexion dédiée, ni super-utilisateur ni BYPASSRLS) : soumis à la RLS ;
--   rag_readonly lecture seule pour un client SQL (migration 003) : voit tout, n'écrit rien.
--
-- Qui appelle : l'API ouvre une transaction par requête et y place l'empreinte SHA-256 du
-- jeton reçu (set_config('app.token_hash', …, true)). Les fonctions ci-dessous retrouvent
-- l'utilisateur à partir de cette empreinte. rag_api ne peut pas lire les empreintes des
-- autres (colonne interdite) : une injection SQL ne peut pas se faire passer pour quelqu'un.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_api') THEN
        -- LOGIN et mot de passe posés au démarrage de l'API (API_DB_PASSWORD, hors du repo)
        CREATE ROLE rag_api NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

CREATE TABLE groups (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE group_members (
    group_id bigint NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    user_id  bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX group_members_user_idx ON group_members (user_id);

CREATE TABLE collection_grants (
    group_id      bigint NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    collection_id bigint NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
    permission    text NOT NULL CHECK (permission IN ('read', 'write')),
    PRIMARY KEY (group_id, collection_id)
);

-- --- Qui appelle ? -------------------------------------------------------------------
-- SECURITY DEFINER : exécutées avec les droits du propriétaire (lisent tokens et users
-- sans RLS). search_path fixé : une fonction de même nom créée ailleurs ne peut pas
-- s'y substituer.

-- Utilisateur du jeton de la transaction ; NULL si aucun jeton valable (aucun droit).
CREATE FUNCTION rag_current_user_id() RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT u.id
    FROM tokens t JOIN users u ON u.id = t.user_id
    WHERE t.token_hash = current_setting('app.token_hash', true)
      AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > now())
      AND u.disabled_at IS NULL
$$;

CREATE FUNCTION rag_is_admin() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT coalesce((SELECT is_admin FROM users WHERE id = rag_current_user_id()), false)
$$;

-- Collections accessibles à l'appelant : min_permission = 'read' (lecture ou écriture) ou
-- 'write'. Un admin a tous les droits sur toutes les collections.
CREATE FUNCTION rag_collections(min_permission text) RETURNS bigint[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT CASE
        WHEN rag_current_user_id() IS NULL THEN '{}'::bigint[]
        WHEN rag_is_admin() THEN (SELECT coalesce(array_agg(id), '{}') FROM collections)
        ELSE (
            SELECT coalesce(array_agg(DISTINCT g.collection_id), '{}')
            FROM group_members m JOIN collection_grants g ON g.group_id = m.group_id
            WHERE m.user_id = rag_current_user_id()
              AND (min_permission = 'read' OR g.permission = 'write')
        )
    END
$$;

-- Vérification d'un jeton par l'API (connexion), sans lui donner accès aux empreintes :
-- renvoie l'utilisateur et note la date d'utilisation.
CREATE FUNCTION rag_token_user(p_token_hash text)
RETURNS TABLE (user_id bigint, username text, is_admin boolean, token_id bigint)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    UPDATE tokens t SET last_used_at = now()
    FROM users u
    WHERE t.token_hash = p_token_hash AND u.id = t.user_id
      AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > now())
      AND u.disabled_at IS NULL
    RETURNING u.id, u.username, u.is_admin, t.id
$$;

REVOKE EXECUTE ON FUNCTION rag_current_user_id(), rag_is_admin(), rag_collections(text),
    rag_token_user(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rag_current_user_id(), rag_is_admin(), rag_collections(text),
    rag_token_user(text) TO rag_api;

-- --- Droits de rag_api : le strict nécessaire, table par table --------------------------
GRANT USAGE ON SCHEMA public TO rag_api;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rag_api;
GRANT SELECT ON schema_migrations TO rag_api;  -- vérifie au démarrage que la base est à jour
-- Connexion : lit les comptes (mot de passe compris, pour le login), crée et révoque des
-- jetons, mais ne lit jamais token_hash (pas de SELECT sur cette colonne).
GRANT SELECT, INSERT, UPDATE (password_hash, is_admin, disabled_at) ON users TO rag_api;
GRANT SELECT (id, user_id, kind, name, prefix, created_at, last_used_at, expires_at, revoked_at),
      INSERT (user_id, kind, name, token_hash, prefix, expires_at),
      UPDATE (revoked_at)
      ON tokens TO rag_api;
GRANT SELECT, INSERT ON collections TO rag_api;
GRANT SELECT, INSERT, DELETE ON groups, group_members TO rag_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON collection_grants TO rag_api;
GRANT SELECT, INSERT, UPDATE ON documents TO rag_api;
GRANT SELECT ON chunks TO rag_api;  -- les chunks sont écrits par le worker (propriétaire)
GRANT INSERT ON jobs TO rag_api;

-- rag_readonly (migration 003) : lecture des nouvelles tables par ses privilèges par défaut ;
-- il ne doit pas exécuter les fonctions de droits (inutiles pour lui).

-- --- Row-Level Security ------------------------------------------------------------------
-- (SELECT fonction()) : évaluée une fois par requête, pas une fois par ligne ; le cast
-- ::bigint[] est nécessaire, sinon « = ANY ((SELECT …)) » est lu comme une sous-requête.

ALTER TABLE collections ENABLE ROW LEVEL SECURITY;
-- « admin OR » : une collection qu'un admin vient de créer doit être visible dans la même
-- requête (INSERT … RETURNING), alors que la liste des collections a été lue avant.
CREATE POLICY api_read ON collections FOR SELECT TO rag_api
    USING ((SELECT rag_is_admin()) OR id = ANY ((SELECT rag_collections('read'))::bigint[]));
CREATE POLICY api_admin_insert ON collections FOR INSERT TO rag_api
    WITH CHECK ((SELECT rag_is_admin()));

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_read ON documents FOR SELECT TO rag_api
    USING (collection_id = ANY ((SELECT rag_collections('read'))::bigint[]));
CREATE POLICY api_insert ON documents FOR INSERT TO rag_api
    WITH CHECK (collection_id = ANY ((SELECT rag_collections('write'))::bigint[]));
CREATE POLICY api_update ON documents FOR UPDATE TO rag_api
    USING (collection_id = ANY ((SELECT rag_collections('write'))::bigint[]))
    WITH CHECK (collection_id = ANY ((SELECT rag_collections('write'))::bigint[]));

ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_read ON chunks FOR SELECT TO rag_api
    USING (collection_id = ANY ((SELECT rag_collections('read'))::bigint[]));

-- Comptes : lisibles par l'API (login), créés et modifiés par un admin seulement
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_read ON users FOR SELECT TO rag_api USING (true);
CREATE POLICY api_admin_insert ON users FOR INSERT TO rag_api
    WITH CHECK ((SELECT rag_is_admin()));
CREATE POLICY api_admin_update ON users FOR UPDATE TO rag_api
    USING ((SELECT rag_is_admin())) WITH CHECK ((SELECT rag_is_admin()));

-- Groupes et droits : réservés aux admins
ALTER TABLE groups ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_admin ON groups FOR ALL TO rag_api
    USING ((SELECT rag_is_admin())) WITH CHECK ((SELECT rag_is_admin()));
ALTER TABLE group_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_admin ON group_members FOR ALL TO rag_api
    USING ((SELECT rag_is_admin())) WITH CHECK ((SELECT rag_is_admin()));
ALTER TABLE collection_grants ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_admin ON collection_grants FOR ALL TO rag_api
    USING ((SELECT rag_is_admin())) WITH CHECK ((SELECT rag_is_admin()));

-- Lecture seule (DBeaver) : voit toutes les lignes des tables qu'il a le droit de lire
CREATE POLICY readonly_all ON collections FOR SELECT TO rag_readonly USING (true);
CREATE POLICY readonly_all ON documents FOR SELECT TO rag_readonly USING (true);
CREATE POLICY readonly_all ON chunks FOR SELECT TO rag_readonly USING (true);
CREATE POLICY readonly_all ON users FOR SELECT TO rag_readonly USING (true);
CREATE POLICY readonly_all ON groups FOR SELECT TO rag_readonly USING (true);
CREATE POLICY readonly_all ON group_members FOR SELECT TO rag_readonly USING (true);
CREATE POLICY readonly_all ON collection_grants FOR SELECT TO rag_readonly USING (true);
