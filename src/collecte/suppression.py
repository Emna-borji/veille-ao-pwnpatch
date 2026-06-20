from pymongo import MongoClient
col = MongoClient("mongodb://localhost:27017")["veille_ao"]["opportunites"]
# remettre à zéro les scores ET le statut pertinent, pour forcer le re-scoring
col.update_many(
    {},
    {"$set": {"score": None, "pertinent": None, "reasoning": None,
              "services": "", "priority": None, "enrichi": False}}
)
print("scores réinitialisés, prêt pour re-scoring")