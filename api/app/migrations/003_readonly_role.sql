-- Rôle de lecture seule pour consulter la base avec un client SQL (DBeaver…), sans risque
-- de modifier ou de supprimer quoi que ce soit. Rôle sans connexion : les comptes qui s'en
-- servent sont créés par « python -m app.admin create-db-reader <nom> » (mot de passe hors
-- du repo, qui est public).

-- Les rôles sont communs à toutes les bases du serveur (rag et rag_test sur le poste de dev) :
-- créé seulement s'il n'existe pas encore.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_readonly') THEN
        CREATE ROLE rag_readonly NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO rag_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rag_readonly;
-- Tables créées plus tard par les migrations (exécutées par le propriétaire du schéma)
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO rag_readonly;

-- Secrets exclus : empreintes des jetons et hachages des mots de passe
REVOKE SELECT ON tokens FROM rag_readonly;
REVOKE SELECT ON users FROM rag_readonly;
GRANT SELECT (id, username, is_admin, disabled_at, created_at) ON users TO rag_readonly;
