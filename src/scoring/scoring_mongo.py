# -*- coding: utf-8 -*-
"""
Module 2 - Scoring de pertinence des appels d'offres.

Pour chaque annonce non encore scorée dans MongoDB :
  1. on envoie au LLM le catalogue de services + l'intitulé de l'annonce ;
  2. le LLM renvoie un score (0-100), une catégorie et une justification ;
  3. on écrit ces champs dans le document.

Pré-requis :
    pip install pymongo openai

Le choix du fournisseur d'IA (et la question de confidentialité du catalogue)
est à valider avec l'encadrante. Tant qu'aucune clé API n'est configurée,
le script tourne en MODE TEST avec une IA simulée, pour pouvoir tester la chaîne.
"""

import os
import re
import json
from datetime import datetime
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
MONGO_URI = "mongodb://localhost:27017"
NOM_BASE = "veille_ao"
NOM_COLLECTION = "opportunites"

# Seuil au-dessus duquel une annonce est jugée pertinente (à ajuster).
SEUIL_PERTINENCE = 50

# ----------------------------------------------------------------------
# Le catalogue de référence (grille de matching).
# À VALIDER avec l'encadrante : c'est le document qui détermine la qualité du tri.
# ----------------------------------------------------------------------
CATALOGUE = """
PWN & PATCH est une entreprise de services en cybersécurité. Son catalogue :
- Tests d'intrusion (pentest) : infrastructure, applications web, mobile, red team
- Audit de sécurité : audit technique et audit organisationnel
- Conformité et GRC : ISO 27001, audit ANCS, gouvernance, gestion des risques
- Gestion des vulnérabilités
- ASM (Attack Surface Management) : surveillance de la surface d'attaque exposée
- Threat intelligence (renseignement sur les menaces)

Ne sont PAS dans le périmètre : développement web/applicatif simple, fourniture
de matériel, travaux de câblage ou d'infrastructure physique, marchés de bâtiment,
mobilier, recrutement.
"""

# ----------------------------------------------------------------------
# Appel au LLM
# ----------------------------------------------------------------------
def construire_prompt(intitule):
    """Construit la consigne envoyée au LLM."""
    return f"""Tu évalues la pertinence d'un appel d'offres pour une entreprise de cybersécurité.

{CATALOGUE}

Appel d'offres à évaluer :
"{intitule}"

Donne un score de 0 à 100 indiquant à quel point cet appel d'offres correspond au
catalogue ci-dessus (100 = correspond parfaitement, 0 = totalement hors périmètre).
Réponds UNIQUEMENT avec un objet JSON, sans texte autour, au format exact :
{{"score": <entier 0-100>, "categorie": "<service du catalogue ou 'hors périmètre'>", "justification": "<une phrase courte>"}}"""


def scorer_avec_ia(intitule):
    """
    Envoie l'intitulé au LLM et renvoie le texte de sa réponse.
    --- POINT À ADAPTER selon le fournisseur validé par l'encadrante. ---
    """
    cle = os.environ.get("OPENAI_API_KEY")

    # MODE TEST : pas de clé -> IA simulée, pour tester la chaîne sans dépense.
    if not cle:
        return _ia_simulee(intitule)

    # MODE REEL : exemple avec OpenAI (adapter le modèle au besoin).
    from openai import OpenAI
    client = OpenAI(api_key=cle)
    reponse = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": construire_prompt(intitule)}],
        temperature=0,
    )
    return reponse.choices[0].message.content

    # --- Variante Anthropic (si c'est le fournisseur retenu) :
    # from anthropic import Anthropic
    # client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    # msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=300,
    #         messages=[{"role": "user", "content": construire_prompt(intitule)}])
    # return msg.content[0].text


def _ia_simulee(intitule):
    """Fausse IA pour le MODE TEST (règles grossières par mots-clés)."""
    t = intitule.lower()
    hors = ["câblage", "fibre", "fourniture", "mobilier", "peinture",
            "climatisation", "véhicule", "bâtiment", "étanchéité", "nettoyage"]
    cyber = ["sécurité", "pentest", "intrusion", "audit", "iso 27001",
             "vulnérabilit", "cyber", "soc"]
    if any(m in t for m in cyber):
        return '{"score": 85, "categorie": "Audit de sécurité", "justification": "Prestation de sécurité correspondant au catalogue."}'
    if any(m in t for m in hors):
        return '{"score": 5, "categorie": "hors périmètre", "justification": "Travaux ou fourniture sans lien avec la cybersécurité."}'
    return '{"score": 30, "categorie": "hors périmètre", "justification": "Informatique générale, pas de la cybersécurité."}'


def parser_reponse(texte):
    """Extrait le JSON de la réponse du LLM (retire d'éventuels ``` autour)."""
    texte = re.sub(r"```json|```", "", texte).strip()
    return json.loads(texte)


# ----------------------------------------------------------------------
# Programme principal
# ----------------------------------------------------------------------
def main():
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
    except ServerSelectionTimeoutError:
        print("Impossible de se connecter à MongoDB.")
        return

    collection = client[NOM_BASE][NOM_COLLECTION]

    if not os.environ.get("OPENAI_API_KEY"):
        print(">>> MODE TEST (IA simulée). Définis OPENAI_API_KEY pour le mode réel.\n")

    # On ne score que les annonces qui n'ont pas encore de score.
    a_scorer = list(collection.find({"score": None}))
    print(f"{len(a_scorer)} annonces à scorer.\n")

    for doc in a_scorer:
        try:
            res = parser_reponse(scorer_avec_ia(doc["intitule"]))
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  [erreur de parsing] {doc['intitule'][:50]} ({e})")
            continue

        collection.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "score": res["score"],
                "categorie": res.get("categorie"),
                "justification": res["justification"],
                "pertinent": res["score"] >= SEUIL_PERTINENCE,
                "score_le": datetime.now().isoformat(timespec="seconds"),
            }},
        )

    # Récapitulatif : les mieux notées d'abord.
    print("Top des annonces par score :\n")
    for doc in collection.find().sort("score", -1).limit(10):
        marque = "✓" if doc.get("pertinent") else " "
        print(f"  {marque} [{doc.get('score'):>3}] {doc['intitule'][:55]}")

    pertinentes = collection.count_documents({"pertinent": True})
    print(f"\n{pertinentes} annonce(s) jugée(s) pertinente(s) (score >= {SEUIL_PERTINENCE}).")


if __name__ == "__main__":
    main()
