# -*- coding: utf-8 -*-
"""
Collecte des appels d'offres de la BCEAO et insertion dans MongoDB.

Aligné sur l'exploration (notebook) :
- récupère TOUTES les annonces (en cours + closes), avec leur section d'origine ;
- la section est lue de façon fiable via le <h2> qui précède chaque annonce ;
- dates normalisées au format ISO.

Stockage avec déduplication ET rafraîchissement :
- une annonce nouvelle est insérée avec son statut de pipeline initial ;
- une annonce déjà connue voit ses infos du site (statut_source, date limite, intitulé)
  rafraîchies, SANS écraser le travail de l'équipe (statut du pipeline, score, historique).

Pré-requis : pip install requests beautifulsoup4 pymongo
"""

import re
import requests
from datetime import datetime, date
from bs4 import BeautifulSoup
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ServerSelectionTimeoutError

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
MONGO_URI = "mongodb://localhost:27017"
NOM_BASE = "veille_ao"
NOM_COLLECTION = "opportunites"

URL = "https://www.bceao.int/fr/appels-offres/appels-offres-marches-publics-achats"
BASE = "https://www.bceao.int"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}

MOIS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


# ----------------------------------------------------------------------
# 1. Collecte + extraction
# ----------------------------------------------------------------------
def normaliser_date(txt):
    """'16 juin 2026' -> '2026-06-16'. Renvoie None si non reconnu."""
    if not txt:
        return None
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", txt.strip().lower())
    if not m:
        return None
    jour, mois, annee = m.groups()
    num = MOIS.get(mois)
    return f"{annee}-{num:02d}-{int(jour):02d}" if num else None


def section_de(a):
    """Section d'une annonce = le <h2> qui la précède (méthode fiable)."""
    h2 = a.find_previous("h2")
    titre = " ".join(h2.get_text().split()).lower() if h2 else ""
    if "en cours" in titre:
        return "en cours"
    if "clos" in titre:
        return "clôturé"
    return "inconnu"


def collecter_bceao():
    """Récupère et extrait toutes les annonces (en cours + closes) de la 1re page."""
    reponse = requests.get(URL, headers=HEADERS, timeout=30)
    reponse.raise_for_status()
    soup = BeautifulSoup(reponse.text, "html.parser")

    annonces_html = [a for a in soup.find_all("a", href=re.compile(r"/fr/appels-offres/[a-z]"))
                     if "ublié le" in a.get_text()]

    motif = re.compile(
        r"Publié le\s+(?P<pub>\d{1,2}\s+\w+\s+\d{4})\s+"
        r"(?P<ref>[A-Z0-9/\-\u00b0N ]+?)?\s*"
        r"Date limite le\s+(?P<lim>\d{1,2}\s+\w+\s+\d{4})\s+"
        r"(?P<titre>.+)",
        re.IGNORECASE,
    )

    resultats = []
    for a in annonces_html:
        texte = " ".join(a.get_text().split())
        m = motif.search(texte)
        if not m:
            continue
        href = a.get("href")
        lien = href if href.startswith("http") else BASE + href
        reference = (m.group("ref") or "").strip()
        if reference and (len(reference) < 4 or reference.lower() == "n"):
            reference = ""
        resultats.append({
            "cle_unique": lien.rstrip("/").split("/")[-1],
            "reference": reference,
            "intitule": m.group("titre").strip(),
            "date_publication": normaliser_date(m.group("pub")),
            "delai_soumission": normaliser_date(m.group("lim")),
            "lien": lien,
            "source": "BCEAO",
            "statut_source": section_de(a),
        })
    return resultats


# ----------------------------------------------------------------------
# 2. Stockage MongoDB
# ----------------------------------------------------------------------
def inserer_ou_rafraichir(collection, annonce):
    """
    Insère si nouvelle, sinon rafraîchit les infos du site sans écraser le travail de l'équipe.
    - $set        : infos du site, toujours mises à jour.
    - $setOnInsert : champs posés une seule fois (pipeline, historique).
    Renvoie True si une nouvelle annonce a été insérée.
    """
    maintenant = datetime.now().isoformat(timespec="seconds")
    resultat = collection.update_one(
        {"cle_unique": annonce["cle_unique"]},
        {
            "$set": {
                "intitule": annonce["intitule"],
                "delai_soumission": annonce["delai_soumission"],
                "statut_source": annonce["statut_source"],
                "maj_le": maintenant,
            },
            "$setOnInsert": {
                "cle_unique": annonce["cle_unique"],
                "reference": annonce["reference"],
                "date_publication": annonce["date_publication"],
                "lien": annonce["lien"],
                "source": annonce["source"],
                "statut": "à étudier",
                "score": None,
                "justification": None,
                "historique": [{"statut": "à étudier", "date": maintenant, "par": "système"}],
                "collecte_le": maintenant,
            },
        },
        upsert=True,
    )
    return resultat.upserted_id is not None


def changer_statut(collection, cle_unique, nouveau_statut, par="utilisateur"):
    """Met à jour le statut du PIPELINE et ajoute une entrée à l'historique."""
    maintenant = datetime.now().isoformat(timespec="seconds")
    collection.update_one(
        {"cle_unique": cle_unique},
        {"$set": {"statut": nouveau_statut},
         "$push": {"historique": {"statut": nouveau_statut, "date": maintenant, "par": par}}},
    )


def est_encore_ouverte(doc):
    """Ouverte si la section dit 'en cours' ET la date limite n'est pas passée."""
    delai = doc.get("delai_soumission")
    date_ok = delai is not None and delai >= date.today().isoformat()
    return doc.get("statut_source") == "en cours" and date_ok


# ----------------------------------------------------------------------
# 3. Programme principal
# ----------------------------------------------------------------------
def main():
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
    except ServerSelectionTimeoutError:
        print("Impossible de se connecter à MongoDB. Vérifie que le serveur tourne.")
        return

    collection = client[NOM_BASE][NOM_COLLECTION]
    collection.create_index([("cle_unique", ASCENDING)], unique=True)

    annonces = collecter_bceao()
    en_cours = sum(1 for a in annonces if a["statut_source"] == "en cours")
    print(f"{len(annonces)} annonces récupérées ({en_cours} en cours, "
          f"{len(annonces) - en_cours} closes).")

    nouvelles = sum(inserer_ou_rafraichir(collection, a) for a in annonces)
    print(f"{nouvelles} nouvelles insérées, {len(annonces) - nouvelles} déjà connues (rafraîchies).")
    print(f"Total en base : {collection.count_documents({})}")

    ouvertes = sum(1 for d in collection.find() if est_encore_ouverte(d))
    print(f"Annonces encore ouvertes (section 'en cours' + date non dépassée) : {ouvertes}")


if __name__ == "__main__":
    main()
