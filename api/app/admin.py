"""Administration des comptes, en ligne de commande (pas d'inscription publique).

Sur le PC fixe, dans WSL :
  bash deploy/compose.sh exec api python -m app.admin create-user lucas --admin
  bash deploy/compose.sh exec api python -m app.admin set-password lucas
  bash deploy/compose.sh exec api python -m app.admin disable-user bob
  bash deploy/compose.sh exec api python -m app.admin list-users
  bash deploy/compose.sh exec api python -m app.admin create-db-reader dbeaver

Le mot de passe est demandé sans être affiché, jamais passé en argument (il resterait dans
l'historique du shell). create-user --no-password : compte sans mot de passe, pour qui
n'utilise qu'une clé API (créée ensuite par un administrateur avec create-api-key).

create-db-reader : compte Postgres en lecture seule (rôle rag_readonly), pour un client SQL ;
relancé sur un compte existant, il en change le mot de passe.

Chaque commande qui modifie quelque chose est inscrite au journal d'accès (action « admin »,
"via": "cli", sans utilisateur : la ligne de commande n'a pas de jeton).
"""

import argparse
import asyncio
import getpass
import re
import sys

import psycopg
from psycopg import sql

from app.audit import record
from app.auth import UserExists, create_user, hash_password, issue_token
from app.config import get_settings
from app.db import migrate

# Nom d'un rôle Postgres créé par create-db-reader (pas de guillemets ni de majuscules)
USERNAME_RE = re.compile(r"[a-z][a-z0-9_]{1,62}")


def ask_password() -> str:
    first = getpass.getpass("Mot de passe : ")
    if getpass.getpass("Encore une fois : ") != first:
        sys.exit("les deux mots de passe diffèrent")
    try:
        hash_password(first)  # vérifie la longueur avant d'aller plus loin
    except ValueError as exc:
        sys.exit(str(exc))
    return first


async def user_id(conn: psycopg.AsyncConnection, username: str) -> int:
    cur = await conn.execute("SELECT id FROM users WHERE username = %s", (username,))
    row = await cur.fetchone()
    if row is None:
        sys.exit(f"utilisateur inconnu : {username}")
    return row[0]


async def run(args: argparse.Namespace) -> None:
    url = get_settings().database_url
    if not url:
        sys.exit("DATABASE_URL manquant")
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await migrate(conn)
        if args.command == "create-user":
            password = None if args.no_password else ask_password()
            try:
                user = await create_user(conn, args.username, password, is_admin=args.admin)
            except UserExists:
                sys.exit(f"l'utilisateur {args.username} existe déjà")
            except psycopg.errors.CheckViolation:
                sys.exit("nom invalide : minuscules, chiffres, « . _ - », 2 à 64 caractères")
            print(f"utilisateur {user.username} créé (id {user.id}, admin : {user.is_admin})")
        elif args.command == "set-password":
            uid = await user_id(conn, args.username)
            password = ask_password()
            await conn.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), uid)
            )
            # Changer de mot de passe ferme les sessions ouvertes (les clés API restent)
            await conn.execute(
                "UPDATE tokens SET revoked_at = now()"
                " WHERE user_id = %s AND kind = 'session' AND revoked_at IS NULL",
                (uid,),
            )
            print(f"mot de passe de {args.username} changé, sessions fermées")
        elif args.command == "disable-user":
            uid = await user_id(conn, args.username)
            await conn.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (uid,))
            print(f"{args.username} désactivé : plus aucun de ses jetons n'est accepté")
        elif args.command == "enable-user":
            uid = await user_id(conn, args.username)
            await conn.execute("UPDATE users SET disabled_at = NULL WHERE id = %s", (uid,))
            print(f"{args.username} réactivé")
        elif args.command == "create-api-key":
            uid = await user_id(conn, args.username)
            issued = await issue_token(conn, uid, kind="api_key", name=args.name)
            print(f"clé API de {args.username} (montrée une seule fois) :\n{issued.token}")
        elif args.command == "create-db-reader":
            if not USERNAME_RE.fullmatch(args.username):
                sys.exit("nom invalide : minuscules, chiffres, « _ », 2 à 63 caractères")
            cur = await conn.execute(
                "SELECT rolsuper OR NOT pg_has_role(oid, 'rag_readonly', 'MEMBER')"
                " FROM pg_roles WHERE rolname = %s",
                (args.username,),
            )
            row = await cur.fetchone()
            exists = row is not None
            if exists and row[0]:
                # Ne jamais toucher à un autre rôle (rag, super-utilisateur : l'API en dépend)
                sys.exit(f"{args.username} existe et n'est pas un compte en lecture seule")
            password = ask_password()
            statement = (
                "ALTER ROLE {} WITH LOGIN PASSWORD {}"
                if exists
                else "CREATE ROLE {} LOGIN PASSWORD {} IN ROLE rag_readonly"
            )
            await conn.execute(
                sql.SQL(statement).format(sql.Identifier(args.username), sql.Literal(password))
            )
            verb = "mot de passe changé pour" if exists else "compte en lecture seule créé :"
            print(f"{verb} {args.username} (base {conn.info.dbname})")
        elif args.command == "list-users":
            cur = await conn.execute(
                "SELECT username, is_admin, disabled_at IS NOT NULL, created_at::date"
                " FROM users ORDER BY username"
            )
            for username, is_admin, disabled, created in await cur.fetchall():
                flags = " ".join(
                    f for f, on in (("admin", is_admin), ("désactivé", disabled)) if on
                )
                print(f"{username:<24} {created}  {flags}")
        if args.command != "list-users":  # arrivé ici : la commande a réussi
            detail = {"op": args.command, "username": args.username, "via": "cli"}
            if args.command == "create-api-key":
                detail["name"] = args.name
            await record(conn, "admin", user_id=None, detail=detail)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="crée un compte (mot de passe demandé)")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true", help="droits d'administration")
    create.add_argument("--no-password", action="store_true", help="clé API seulement")
    for name, text in (
        ("set-password", "change le mot de passe et ferme les sessions"),
        ("disable-user", "désactive un compte"),
        ("enable-user", "réactive un compte"),
    ):
        sub.add_parser(name, help=text).add_argument("username")
    key = sub.add_parser("create-api-key", help="crée une clé API pour un utilisateur")
    key.add_argument("username")
    key.add_argument("--name", default="créée par admin")
    sub.add_parser("list-users", help="liste les comptes")
    reader = sub.add_parser(
        "create-db-reader",
        help="compte Postgres en lecture seule (DBeaver), ou nouveau mot de passe",
    )
    reader.add_argument("username")
    args = parser.parse_args()
    args.username = getattr(args, "username", "").strip().lower() or None
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
