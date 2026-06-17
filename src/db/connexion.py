"""
Configuration MongoDB partagée.

Centralise la connexion à la base pour que la collecte et le scoring
utilisent la même configuration (au lieu de redéfinir MONGO_URI chacun
de leur côté). À brancher dans collecte_bceao.py et scoring.py plus tard.

L'URI peut être surchargée par la variable d'environnement MONGO_URI :
    - en local :  mongodb://localhost:27017
    - sur Atlas : mongodb+srv://<user>:<password>@<cluster>.mongodb.net
"""

import os

from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
NOM_BASE = "veille_ao"
NOM_COLLECTION = "opportunites"


def get_collection():
    """
    Renvoie la collection MongoDB des opportunités, ou None si la base
    est injoignable (le message d'erreur est affiché à l'appelant).
    """
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except ServerSelectionTimeoutError:
        print("Impossible de se connecter à MongoDB. Vérifie que le serveur tourne.")
        return None
    return client[NOM_BASE][NOM_COLLECTION]
