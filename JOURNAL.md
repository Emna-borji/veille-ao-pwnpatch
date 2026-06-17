# Journal de bord — Projet veille AO (PWN & PATCH)

## Objectif du projet
Automatiser la veille des appels d'offres : collecter, qualifier (scoring IA),
et suivre les opportunités pertinentes pour les services de cybersécurité de PWN & PATCH.

---

## Décisions importantes

### Sources
- La liste officielle contient beaucoup de sources non collectables (banques/assurances
  privées sans page publique, TUNEPS avec certificat). Documenté dans Etat_sources.xlsx.
- Source pilote retenue : **BCEAO** (publique, structurée, accessible avec requests).
- À valider avec l'encadrante : périmètre géographique, accès aux plateformes authentifiées.

### Base de données
- Choix imposé : **MongoDB**.
- Une collection `opportunites`. L'historique des statuts est embarqué dans chaque document.
- Déduplication via un index unique sur `cle_unique` (le slug de l'URL).

### Collecte
- BCEAO : contenu dans le HTML brut, pas besoin de navigateur headless.
- Section lue via le <h2> précédent (find_previous), pas via find_next_sibling (corrigé).
- Statut croisé : section du site (statut_source) + comparaison de la date limite.

### Scoring
- Approche : LLM avec un prompt qui renvoie score + justification en JSON.
- Catalogue de référence à valider avec l'encadrante.
- Fournisseur LLM à confirmer (budget + confidentialité du catalogue).

---

## Stratégie de construction
1. Chaîne complète sur UNE source (BCEAO) d'abord, puis élargir.
2. Stratégie simple : stocker le lien de détail ; n'extraire les documents que pour
   les annonces jugées pertinentes.
3. Ajouter les autres sources progressivement (faciles, puis difficiles, puis authentifiées).

---

## Questions en attente pour l'encadrante
- Périmètre géographique réel ?
- Accès aux plateformes authentifiées (TUNEPS, banques) ?
- Quel fournisseur LLM ? Contraintes de confidentialité ?

---

## Suivi (à dater)
- [date] : exploration BCEAO terminée, collecte + base + scoring fonctionnels.
- ...