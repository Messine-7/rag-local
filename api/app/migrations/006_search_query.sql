-- Phase 5.4 : question reformulée pour la recherche (questions de suivi). Seulement sur les
-- messages de l'utilisateur, et seulement quand la question a été reformulée.
-- Les droits de rag_api (SELECT, INSERT) et de rag_readonly portent sur toute la table :
-- la nouvelle colonne en hérite.

ALTER TABLE messages ADD COLUMN search_query text;
