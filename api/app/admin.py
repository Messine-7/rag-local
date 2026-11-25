"""Administration des comptes, en ligne de commande (pas d'inscription publique).

Sur le PC fixe, dans WSL :
  bash deploy/compose.sh exec api python -m app.admin create-user lucas --admin
  bash deploy/compose.sh exec api python -m app.admin set-password lucas
  bash deploy/compose.sh exec api python -m app.admin disable-user bob
  bash deploy/compose.sh exec api python -m app.admin list-users

Le mot de passe est demandé sans être affiché, jamais passé en argument (il resterait dans
l'historique du shell). create-user --no-password : compte sans mot de passe, pour qui
n'utilise qu'une clé API (créée ensuite par un administrateur avec create-api-key).
"""

import argparse
import asyncio
import getpass
import sys

import psycopg

from app.auth import UserExists, create_user, hash_password, issue_token
from app.config import get_settings
from app.db import migrate


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
    args = parser.parse_args()
    args.username = getattr(args, "username", "").strip().lower() or None
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
