# -*- coding: utf-8 -*-
"""
Collecte BCEAO + scoring (Ollama local) + stockage MongoDB.

Principes :
- on STOCKE TOUT (en cours ET clôturées) -> permet les KPIs (total détecté) ;
- on ne SCORE que les annonces EN COURS et pas encore scorées (les clôturées
  sont des opportunités mortes : inutile de les évaluer) ;
- la pertinence est un champ calculé (score >= SEUIL) ;
- le digest (côté n8n) lira seulement les pertinentes EN COURS non encore envoyées.

clé unique = domaine::slug  (le domaine vient de l'URL, donc unique par source ;
le slug est unique par site -> pas de collision, et ça reste lisible).

Pré-requis : pip install requests beautifulsoup4 pymongo
Ollama doit tourner en local (http://localhost:11434) avec le modèle llama3.1.
"""

import re
import json
import requests
import time
from urllib.parse import urlparse
from datetime import datetime, date
from bs4 import BeautifulSoup
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ServerSelectionTimeoutError

# ---------------------------------------------------------------- config
MONGO_URI = "mongodb://localhost:27017"
NOM_BASE = "veille_ao"
NOM_COLLECTION = "opportunites"

URL = "https://www.bceao.int/fr/appels-offres/appels-offres-marches-publics-achats"
BASE = "https://www.bceao.int"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODELE = "llama3.1"
SEUIL_PERTINENCE = 55   # un achat de produit (20-50) n'est pas pertinent ; un service de sécurité l'est

MOIS = {"janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,
        "juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,
        "décembre":12,"decembre":12}

CATALOGUE = """PWN & PATCH, société de conseil en cybersécurité (prestations de service), propose :

1. Sécurité offensive (Offensive Security) :
   - Red Team (simulation d'attaques réalistes et avancées)
   - Tests d'intrusion d'infrastructure (Infrastructure Penetration Testing)
   - Tests d'intrusion d'applications Web, mobiles et desktop
   - Évaluation et analyse de vulnérabilités

2. Sécurité du cloud (Cloud Security) sur AWS, Azure et GCP :
   - Audit / évaluation de sécurité cloud (gap analysis, détection de mauvaises configurations)
   - Conception et mise en oeuvre d'architectures cloud sécurisées
   - Stratégie IAM, protection des données, monitoring, DevSecOps, conformité cloud

3. Sécurité de l'information / GRC (gouvernance, risque, conformité) :
   - Conformité réglementaire (ISO 27001, RGPD, directives ANCS)
   - Évaluations des risques, cadres sur mesure, support de conformité

4. Threat Intelligence et surveillance via la plateforme Oktoboot :
   - Renseignement sur les menaces (CTI), surveillance du dark web
   - Détection de fuites de données et d'identifiants compromis
   - Cartographie et gestion de la surface d'attaque (ASM)"""


# ---------------------------------------------------------------- helpers
def normaliser_date(txt):
    if not txt:
        return None
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", txt.strip().lower())
    if not m:
        return None
    jour, mois, annee = m.groups()
    num = MOIS.get(mois)
    return f"{annee}-{num:02d}-{int(jour):02d}" if num else None


def faire_cle_unique(lien):
    """domaine::slug -> unique par source et lisible."""
    domaine = urlparse(lien).netloc.replace("www.", "")
    slug = lien.rstrip("/").split("/")[-1]
    return f"{domaine}::{slug}"


def section_de(a):
    h2 = a.find_previous("h2")
    titre = " ".join(h2.get_text().split()).lower() if h2 else ""
    if "en cours" in titre:
        return "en cours"
    if "clos" in titre:
        return "clôturé"
    return "inconnu"



# ---------------------------------------------------------------- collecte TED
TED_URL = "https://api.ted.europa.eu/v3/notices/search"
CODES_CPV_CYBER = [
    # audit et test informatique
    "72800000", "72810000", "72820000", "72140000", "72150000",
    "72254000", "72254100", "72226000",
    # logiciels et services de sécurité des données
    "48730000", "48731000", "48732000",
    "72212730", "72212731", "72212732", "72212984",
    # antivirus, sauvegarde, reprise après sinistre
    "48760000", "48761000", "48710000",
    "72212760", "72212761", "72212710", "72251000",
]

def _date_ted(valeur):
    if not valeur:
        return None
    if isinstance(valeur, list):
        valeur = valeur[0] if valeur else None
    return str(valeur)[:10] if valeur else None

def _texte_multilingue(champ, langues=("eng", "fra")):
    if isinstance(champ, dict):
        for lang in langues:
            if champ.get(lang):
                v = champ[lang]
                return v[0] if isinstance(v, list) else v
        for v in champ.values():
            return v[0] if isinstance(v, list) else v
    return ""

def _appel_ted(query, champs, page, limit=250):
    """Un appel TED robuste (réessaie si l'API bloque). Renvoie le JSON ou None."""
    body = {"query": query, "fields": champs, "limit": limit, "page": page, "scope": "ACTIVE"}
    for _ in range(3):
        try:
            r = requests.post(TED_URL, json=body, timeout=60)
            if r.status_code == 200:
                return r.json()
            time.sleep(2)
        except Exception:
            time.sleep(2)
    return None

def collecter_ted(jours=None, max_pages=50):
    """Collecte les annonces cyber ACTIVES sur TED, au format de la base.

    Critère principal : l'annonce est ACTIVE (encore ouverte), pas sa date de
    publication. Le filtre de date est donc OPTIONNEL :
    - jours=None  -> toutes les annonces actives (utile au premier remplissage) ;
    - jours=N     -> seulement celles publiées dans les N derniers jours (quotidien).
    La pagination récupère jusqu'à 250 annonces par page (max_pages pages)."""
    filtre_cpv = " OR ".join(f"classification-cpv={c}" for c in CODES_CPV_CYBER)
    filtre_mots = ('notice-title ~ "cybersecurity" OR notice-title ~ "penetration" '
                   'OR notice-title ~ "ISO 27001" OR notice-title ~ "security audit" '
                   'OR notice-title ~ "vulnerability" OR notice-title ~ "pentest" '
                   'OR notice-title ~ "cybersécurité" OR notice-title ~ "cyberbezpieczeństwo" '
                   'OR notice-title ~ "ciberseguridad" OR notice-title ~ "Cybersicherheit" '
                   'OR notice-title ~ "Penetrationstest"')
    query = f"({filtre_cpv} OR {filtre_mots})"
    if jours:
        query += f" AND publication-date>=today(-{jours})"
    query += " AND notice-type=cn-standard SORT BY publication-date DESC"
    champs = ["publication-number", "notice-title", "buyer-name", "buyer-country",
              "publication-date", "deadline", "classification-cpv"]

    auj = date.today().isoformat()
    resultats = []
    for page in range(1, max_pages + 1):
        data = _appel_ted(query, champs, page)
        if not data:
            break
        notices = data.get("notices", [])
        if not notices:
            break
        for n in notices:
            num = n.get("publication-number")
            intitule = _texte_multilingue(n.get("notice-title", {}))
            if not num or not intitule:
                continue
            deadline = _date_ted(n.get("deadline"))
            pays = n.get("buyer-country")
            if isinstance(pays, list):
                pays = pays[0] if pays else None
            # critère "encore ouverte" : clôturé si date limite dépassée, sinon en cours
            statut = "clôturé" if (deadline and deadline < auj) else "en cours"
            resultats.append({
                "cle_unique": f"ted::{num}",
                "reference": num,
                "intitule": intitule,
                "date_publication": _date_ted(n.get("publication-date")),
                "delai_soumission": deadline,
                "lien": f"https://ted.europa.eu/fr/notice/{num}",
                "source": "TED",
                "buyer": _texte_multilingue(n.get("buyer-name", {})),
                "pays": pays,
                "statut_source": statut,
            })
        # si on a tout récupéré, inutile de paginer plus loin
        if len(notices) < 250:
            break
        time.sleep(1)  # politesse envers l'API entre les pages
    return resultats

# ---------------------------------------------------------------- collecte
def collecter_bceao():
    reponse = requests.get(URL, headers=HEADERS, timeout=30)
    reponse.raise_for_status()
    soup = BeautifulSoup(reponse.text, "html.parser")
    annonces_html = [a for a in soup.find_all("a", href=re.compile(r"/fr/appels-offres/[a-z]"))
                     if "ublié le" in a.get_text()]
    motif = re.compile(
        r"Publié le\s+(?P<pub>\d{1,2}\s+\w+\s+\d{4})\s+"
        r"(?P<ref>[A-Z0-9/\-\u00b0N ]+?)?\s*"
        r"Date limite le\s+(?P<lim>\d{1,2}\s+\w+\s+\d{4})\s+"
        r"(?P<titre>.+)", re.IGNORECASE)
    resultats = []
    for a in annonces_html:
        texte = " ".join(a.get_text().split())
        m = motif.search(texte)
        if not m:
            continue
        href = a.get("href")
        lien = href if href.startswith("http") else BASE + href
        ref = (m.group("ref") or "").strip()
        if ref and (len(ref) < 4 or ref.lower() == "n"):
            ref = ""
        resultats.append({
            "cle_unique": faire_cle_unique(lien),
            "reference": ref,
            "intitule": m.group("titre").strip(),
            "date_publication": normaliser_date(m.group("pub")),
            "delai_soumission": normaliser_date(m.group("lim")),
            "lien": lien,
            "source": "BCEAO",
            "statut_source": section_de(a),
        })
    return resultats


# ---------------------------------------------------------------- scoring
def construire_prompt(intitule):
    return f"""{CATALOGUE}

PWN & PATCH est un cabinet de CONSEIL : il VEND DES PRESTATIONS DE SERVICE intellectuelles
(audit, test d'intrusion, conseil, formation, réponse à incident). Il NE VEND PAS de produits,
de licences, de logiciels, ni de matériel.

Analyse l'appel d'offres ci-dessous en suivant ces règles, dans l'ordre :

1) SUJET : l'annonce concerne-t-elle vraiment la SÉCURITÉ informatique ? Si le sujet principal est
   autre chose (santé, bâtiment, transport, informatique générale sans dimension sécurité...),
   alors elle n'est PAS pertinente, même si le mot "cyber" ou "sécurité" apparaît au passage.

2) SERVICE ou ACHAT : l'annonce demande-t-elle une PRESTATION DE SERVICE (réaliser un audit, un
   pentest, du conseil, de la formation) ou l'ACHAT D'UN PRODUIT (acheter/installer des licences,
   un logiciel, un antivirus, des serveurs, du matériel, des équipements) ?
   - Prestation de service de sécurité -> PERTINENT.
   - Simple achat/fourniture de produit ou de licence -> PEU ou PAS pertinent (ce n'est pas le
     métier de PWN & PATCH), même si le produit est un produit de sécurité.

3) SCORE, gradué selon la correspondance réelle :
   - 85-100 : prestation directe du cœur de métier (audit de sécurité, test d'intrusion, conseil
     en cybersécurité, mission de conformité ISO 27001/GRC, réponse à incident).
   - 55-80  : service lié à la sécurité informatique mais plus indirect (infogérance avec volet
     sécurité, surveillance, formation cyber).
   - 20-50  : achat de produit/licence/matériel de sécurité (pas une prestation de conseil).
   - 0-15   : hors sujet (pas de la sécurité, ou pas de l'informatique).
   relevant = true uniquement si score >= 55.

Exemples :
- "Mission d'audit de sécurité du système d'information" -> service, cœur de métier -> score ~90, relevant true.
- "Acquisition et installation de licences antivirus" -> achat de produit -> score ~30, relevant false.
- "Services de santé pour le ministère" -> hors sujet -> score ~5, relevant false.

Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour, au format exact :
{{"relevant": true/false, "score": 0-100, "services": ["services PWN & PATCH réellement concernés, [] si aucun"], "reasoning": "court, en français, justifie le score"}}

Appel d'offres : "{intitule}"
"""


def parser_reponse(texte):
    if not texte:
        return None
    t = texte.strip().replace("```json", "").replace("```", "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        return None
    try:
        d = json.loads(t[i:j+1])
    except Exception:
        return None
    score = int(d.get("score", 0)) if str(d.get("score", "")).strip() != "" else 0
    # la priorité est DÉDUITE du score (cohérente et reproductible), pas décidée par le modèle
    if score >= 80:
        priorite = "haute"
    elif score >= SEUIL_PERTINENCE:
        priorite = "moyenne"
    else:
        priorite = "faible"
    return {
        "relevant": bool(d.get("relevant", False)),
        "score": score,
        "services": d.get("services", []),
        "reasoning": d.get("reasoning", ""),
        "priority": priorite,
    }


def scorer_ollama(intitule):
    payload = {"model": OLLAMA_MODELE,
               "messages": [{"role": "user", "content": construire_prompt(intitule)}],
               "stream": False, "format": "json", "options": {"temperature": 0}}
    # 2 tentatives : Ollama local peut être lent selon la charge de la machine
    for tentative in range(2):
        try:
            r = requests.post(OLLAMA_URL, json=payload, timeout=300)
            r.raise_for_status()
            contenu = r.json().get("message", {}).get("content", "")
            return parser_reponse(contenu)
        except Exception as e:
            if tentative == 0:
                print(f"  [Ollama lent, nouvelle tentative] {str(e)[:60]}")
                time.sleep(3)
            else:
                print(f"  [erreur Ollama, annonce ignorée] {str(e)[:60]}")
                return None


# ---------------------------------------------------------------- mongo
def inserer_ou_rafraichir(collection, a):
    maintenant = datetime.now().isoformat(timespec="seconds")
    res = collection.update_one(
        {"cle_unique": a["cle_unique"]},
        {"$set": {"intitule": a["intitule"], "delai_soumission": a["delai_soumission"],
                  "statut_source": a["statut_source"], "maj_le": maintenant},
         "$setOnInsert": {"cle_unique": a["cle_unique"], "reference": a["reference"],
                          "date_publication": a["date_publication"], "lien": a["lien"],
                          "source": a["source"], "buyer": a.get("buyer"), "pays": a.get("pays"),
                          "statut": "à étudier",
                          "score": None, "pertinent": None, "services": None,
                          "reasoning": None, "priority": None,
                          "sent_in_digest": False, "collecte_le": maintenant,
                          "historique": [{"statut": "à étudier", "date": maintenant, "par": "système"}]}},
        upsert=True)
    return res.upserted_id is not None


def marquer_cloturees(collection):
    """Repasse en 'clôturé' les annonces 'en cours' dont la date limite est dépassée.

    Nécessaire car TED ne renvoie plus les annonces clôturées (scope ACTIVE) :
    sans ce nettoyage, une annonce vue 'en cours' resterait figée ainsi pour toujours.
    Limite : ne corrige que celles qui ONT une date limite (delai_soumission non nul) ;
    celles sans date limite ne peuvent pas être tranchées ici."""
    auj = date.today().isoformat()
    res = collection.update_many(
        {"statut_source": "en cours",
         "delai_soumission": {"$ne": None, "$lt": auj}},
        {"$set": {"statut_source": "clôturé",
                  "maj_le": datetime.now().isoformat(timespec="seconds")}})
    print(f"{res.modified_count} annonce(s) dont la date limite est dépassée repassée(s) en clôturé.")


def scorer_les_nouvelles(collection):
    """Score uniquement les annonces EN COURS pas encore scorées.
    Les clôturées sont gardées (KPIs) mais jamais scorées."""
    a_scorer = list(collection.find({"score": None, "statut_source": "en cours"}))
    print(f"{len(a_scorer)} annonce(s) en cours à scorer.")
    for doc in a_scorer:
        r = scorer_ollama(doc["intitule"])
        if r is None:
            continue
        pertinent = r["score"] >= SEUIL_PERTINENCE
        services = r["services"]
        if isinstance(services, list):
            services = ", ".join(str(s) for s in services)
        collection.update_one(
            {"cle_unique": doc["cle_unique"]},
            {"$set": {"score": r["score"], "pertinent": pertinent, "services": services,
                      "reasoning": r["reasoning"], "priority": r["priority"],
                      "score_le": datetime.now().isoformat(timespec="seconds")}})
        print(f"  [{r['score']:3}] {'PERTINENT' if pertinent else 'non      '} | {doc['intitule'][:55]}")


def _description_ted(champ):
    """Description dans la langue disponible : français d'abord, sinon anglais,
    sinon n'importe quelle langue (les descriptions TED ne sont pas traduites)."""
    if isinstance(champ, dict) and champ:
        for lg in ("fra", "eng"):
            if champ.get(lg):
                v = champ[lg]
                return (v[0] if isinstance(v, list) else v), lg
        for lg, v in champ.items():       # repli : n'importe quelle langue
            return (v[0] if isinstance(v, list) else v), lg
    return None, None


def enrichir_pertinentes(collection, max_desc=1500):
    """Récupère les détails (description, procédure, date limite fiable, prix) pour les
    annonces TED PERTINENTES pas encore enrichies. Stocke ce qui est présent ; les
    champs absents restent à None (une autre annonce pourra les avoir)."""
    a_enrichir = list(collection.find(
        {"source": "TED", "pertinent": True, "enrichi": {"$ne": True}}))
    if not a_enrichir:
        print("Aucune annonce pertinente à enrichir.")
        return
    print(f"{len(a_enrichir)} annonce(s) pertinente(s) à enrichir.")

    refs = [d["reference"] for d in a_enrichir if d.get("reference")]
    champs = ["publication-number", "description-proc", "description-lot",
              "procedure-type", "deadline-receipt-tender-date-lot",
              "estimated-value-lot", "estimated-value-cur-lot", "main-activity"]

    # on interroge l'API par lots de 200 (max 250), scope ALL pour enrichir même les clôturées
    details = {}
    for i in range(0, len(refs), 200):
        lot = refs[i:i+200]
        query = " OR ".join(f'publication-number="{n}"' for n in lot)
        data = None
        for _ in range(3):
            try:
                r = requests.post(TED_URL, json={"query": query, "fields": champs,
                                  "limit": 200, "page": 1, "scope": "ALL"}, timeout=60)
                if r.status_code == 200:
                    data = r.json(); break
                time.sleep(2)
            except Exception:
                time.sleep(2)
        if not data:
            print("  TED injoignable pour ce lot, on réessaiera plus tard.")
            continue
        for n in data.get("notices", []):
            details[n.get("publication-number")] = n
        time.sleep(1)

    # mettre à jour chaque annonce avec ce qu'on a trouvé
    enrichies = 0
    for d in a_enrichir:
        n = details.get(d.get("reference"))
        if n is None:
            continue   # pas reçue cette fois -> on retentera au prochain passage
        desc, langue = _description_ted(n.get("description-proc") or n.get("description-lot"))
        if desc:
            desc = desc[:max_desc]
        # date limite plus fiable que le champ deadline initial
        dl = n.get("deadline-receipt-tender-date-lot")
        if isinstance(dl, list):
            dl = dl[0] if dl else None
        dl = str(dl)[:10] if dl else None
        # prix (souvent absent) + devise
        prix = n.get("estimated-value-lot")
        if isinstance(prix, list):
            prix = prix[0] if prix else None
        devise = n.get("estimated-value-cur-lot")
        if isinstance(devise, list):
            devise = devise[0] if devise else None
        proc = n.get("procedure-type")
        if isinstance(proc, list):
            proc = proc[0] if proc else None

        maj = {"description": desc, "description_langue": langue,
               "procedure": proc, "prix_estime": prix, "devise": devise,
               "enrichi": True, "enrichi_le": datetime.now().isoformat(timespec="seconds")}
        if dl:   # si une date limite plus fiable existe, on l'utilise
            maj["delai_soumission"] = dl
        collection.update_one({"cle_unique": d["cle_unique"]}, {"$set": maj})
        enrichies += 1
        print(f"  enrichie [{langue or '--'}] {d['intitule'][:50]}")
    print(f"{enrichies} annonce(s) enrichie(s).")


def changer_statut(collection, cle_unique, nouveau_statut, par="utilisateur"):
    maintenant = datetime.now().isoformat(timespec="seconds")
    collection.update_one(
        {"cle_unique": cle_unique},
        {"$set": {"statut": nouveau_statut},
         "$push": {"historique": {"statut": nouveau_statut, "date": maintenant, "par": par}}})


# ---------------------------------------------------------------- main
def main():
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
    except ServerSelectionTimeoutError:
        print("MongoDB injoignable. Vérifie que le serveur tourne.")
        return
    collection = client[NOM_BASE][NOM_COLLECTION]
    collection.create_index([("cle_unique", ASCENDING)], unique=True)

    annonces = collecter_bceao() + collecter_ted(jours=7)
    en_cours = sum(1 for a in annonces if a["statut_source"] == "en cours")
    print(f"{len(annonces)} annonces collectées ({en_cours} en cours, {len(annonces)-en_cours} closes).")
    nouvelles = sum(inserer_ou_rafraichir(collection, a) for a in annonces)
    print(f"{nouvelles} nouvelles, {len(annonces) - nouvelles} déjà connues.")

    marquer_cloturees(collection)
    scorer_les_nouvelles(collection)
    enrichir_pertinentes(collection)

    total = collection.count_documents({})
    pertinentes = collection.count_documents({"pertinent": True})
    a_envoyer = collection.count_documents({"pertinent": True, "statut_source": "en cours",
                                            "sent_in_digest": False})
    print(f"\nTotal détecté : {total} | pertinentes : {pertinentes} | à envoyer : {a_envoyer}")


if __name__ == "__main__":
    main()
