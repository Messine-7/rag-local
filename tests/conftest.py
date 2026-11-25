import asyncio
import sys

# Poste de dev Windows : psycopg en mode async ne fonctionne pas avec la boucle par défaut
# (Proactor). Sans effet sous Linux (CI, image Docker).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
