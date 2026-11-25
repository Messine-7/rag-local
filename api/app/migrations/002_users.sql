-- Phase 5.1 : utilisateurs et jetons d'accès. Chaque requête à l'API est rattachée à un
-- utilisateur (voir app/auth.py). Pas d'inscription publique : comptes créés par app/admin.py.

CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9._-]{1,63}$'),
    password_hash text,  -- argon2 ; NULL : pas de connexion par mot de passe (clé API seulement)
    is_admin      boolean NOT NULL DEFAULT false,
    disabled_at   timestamptz,  -- compte désactivé : plus aucun jeton n'est accepté
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Jetons de session (connexion par mot de passe, durée limitée) et clés API personnelles
-- (CLI, scripts ; valables jusqu'à révocation). Seule l'empreinte SHA-256 est stockée : une
-- fuite de la base ne donne aucun jeton utilisable.
CREATE TABLE tokens (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('session', 'api_key')),
    name         text,  -- nom donné à une clé API (« laptop », « bench »…)
    token_hash   text NOT NULL UNIQUE,
    prefix       text NOT NULL,  -- début du jeton, pour reconnaître une clé dans une liste
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at   timestamptz,  -- NULL : n'expire pas (clé API)
    revoked_at   timestamptz
);
CREATE INDEX tokens_user_idx ON tokens (user_id);

-- Qui a envoyé chaque document (NULL pour ceux envoyés avant la connexion)
ALTER TABLE documents ADD COLUMN created_by bigint REFERENCES users (id);
