-- Base des tests, dans le même Postgres que la base de dev « rag ».
-- Les tests la vident à chaque fois ; le worker de dev ne lit que « rag ».
-- Exécuté par l'image Postgres au premier démarrage du volume pgdata seulement. Volume déjà
-- créé : docker compose exec db createdb -U rag rag_test
CREATE DATABASE rag_test;
