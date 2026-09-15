"""Lancement du service en développement : `python run.py`.

Pourquoi un point d'entrée dédié plutôt qu'un simple `uvicorn app.main:app` ?

Sous Windows, la boucle d'événements par défaut est `ProactorEventLoop`, que
psycopg 3 refuse en mode asynchrone. Il faut donc démarrer le serveur sur une
`SelectorEventLoop`. Cela ne peut pas se faire depuis `app/main.py` : uvicorn
crée la boucle *avant* d'importer l'application.

Sous Linux — l'environnement de déploiement — ce fichier ne change rien, et
`uvicorn app.main:app` reste utilisable directement.
"""

import asyncio
import sys

import uvicorn

from app.core.config import settings


def main() -> None:
    config = uvicorn.Config(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=not settings.is_production,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
