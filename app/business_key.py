"""Identite STABLE d'un document physique, independante de Gmail.

Pourquoi ce module existe : l'identifiant de piece jointe rendu par Gmail
tourne. Le meme PDF, relu a deux cycles d'intervalle, recevait donc deux
`doc_key` differents - et la base a fini par contenir ~94 fiches pour un
seul document. Tant que la deduplication se faisait sur `doc_key`, elle ne
pouvait rien voir : chaque fiche etait "nouvelle".

La cle metier ci-dessous ne regarde jamais l'`attachment_id`. Elle
descend une echelle de fiabilite et s'arrete au premier barreau
disponible :

  1. l'empreinte SHA-256 du contenu reel de la piece. Deux fichiers
     identiques ont la meme empreinte, quel que soit leur chemin ;
  2. a defaut, le message Gmail + le nom de fichier normalise + la
     taille. Le message ne tourne pas, lui ;
  3. en dernier recours, l'identite COMPTABLE du document : type,
     numero, tiers, date, montant. C'est ce qu'un comptable utiliserait
     pour dire "c'est la meme facture".

Aucun barreau n'est devine : si le niveau 3 lui-meme manque de matiere,
la fonction rend une chaine vide et l'appelant DOIT traiter la fiche
comme unique. Fusionner deux documents sur une identite incertaine
serait pire que d'en garder deux.
"""
from __future__ import annotations

import re
import unicodedata

# Prefixes lisibles : en lisant une cle dans un log, on sait immediatement
# sur quel niveau de preuve la deduplication s'est appuyee.
LEVEL_SHA = "sha256"
LEVEL_GMAIL = "gmail"
LEVEL_BUSINESS = "metier"


def normalize(value: str | None) -> str:
    """Minuscules, sans accents, sans ponctuation, espaces reduits.

    "Facture_TEST 2026-003.pdf" et "facture test 2026 003.pdf" designent
    le meme fichier pour un humain : ils doivent donner la meme cle.
    """
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^0-9A-Za-z]+", " ", text)
    return " ".join(text.lower().split())


def business_document_key(fiche: dict) -> str:
    """Cle stable d'un document physique. Vide si rien de fiable.

    `fiche` est une ligne de la table `documents` (ou tout dict portant
    les memes champs).
    """
    sha = str(fiche.get("file_sha256") or "").strip().lower()
    if sha:
        return f"{LEVEL_SHA}:{sha}"

    message = str(fiche.get("gmail_message_id") or "").strip()
    nom = normalize(
        fiche.get("member_path") or fiche.get("filename") or ""
    )
    taille = str(fiche.get("file_size") or "").strip()
    if message and nom:
        # La taille complete le couple quand elle est connue ; son absence
        # n'invalide pas le niveau, elle le rend seulement moins precis.
        return f"{LEVEL_GMAIL}:{message}|{nom}|{taille}"

    # Niveau 3 : identite comptable. On exige le numero ET au moins un
    # autre element, sinon deux factures differentes du meme fournisseur
    # se confondraient.
    numero = normalize(fiche.get("numero"))
    if not numero:
        return ""
    parts = [
        normalize(fiche.get("doc_type")),
        numero,
        normalize(fiche.get("party_id") or fiche.get("tiers")),
        normalize(fiche.get("date_document")),
        normalize(fiche.get("montant_ttc")),
    ]
    if not any(parts[i] for i in (0, 2, 3, 4)):
        return ""
    return f"{LEVEL_BUSINESS}:" + "|".join(parts)


def business_identity(fiche: dict) -> str:
    """L'identite COMPTABLE seule : ni empreinte, ni identifiant Gmail.

    `business_document_key` s'arrete au premier barreau disponible, donc
    presque toujours sur l'empreinte du fichier. C'est ce qu'on veut pour
    reconnaitre un fichier identique - mais pas pour reconnaitre un MEME
    DOCUMENT arrive sous deux fichiers differents (re-export, re-scan,
    tampon appose). Ici on force la descente au niveau metier.

    Rend une chaine vide si l'identite comptable n'est pas etablie : dans
    le doute, deux documents restent deux documents.
    """
    allege = {
        cle: valeur for cle, valeur in fiche.items()
        if cle not in ("file_sha256", "gmail_message_id")
    }
    cle = business_document_key(allege)
    return cle if cle.startswith(f"{LEVEL_BUSINESS}:") else ""


def completeness(fiche: dict) -> tuple:
    """Score de completude, pour choisir la fiche CANONIQUE d'un groupe.

    On garde celle qui renseigne le mieux le comptable : un lien Drive
    vivant d'abord, puis un motif d'anomalie detaille, puis une ligne de
    journal, puis l'anteriorite. Trier sur ce tuple decroissant fait
    remonter la meilleure.

    L'anteriorite arrive en dernier et NON en premier : la plus ancienne
    fiche n'est pas forcement la mieux remplie, et c'est la richesse de
    l'information qui compte pour qui doit trancher.
    """
    return (
        1 if str(fiche.get("drive_link") or "").strip() else 0,
        len(str(fiche.get("payload") or "")),
        1 if int(fiche.get("log_row") or 0) else 0,
        # created_at croissant = plus ancien prefere, d'ou la negation
        # via tri inverse sur la chaine complementee.
        _anciennete(str(fiche.get("created_at") or "")),
    )


def _anciennete(created_at: str) -> str:
    """Cle de tri qui fait remonter la fiche la PLUS ANCIENNE.

    Les horodatages sont ISO, donc comparables lexicalement. On veut le
    minimum alors que le tri global cherche un maximum : on complemente
    chaque caractere pour inverser l'ordre.
    """
    if not created_at:
        return ""
    return "".join(chr(0x10FFFD - ord(c)) if ord(c) < 0x10FFFD else c
                   for c in created_at)


def group_by_business_key(fiches: list[dict]) -> list[tuple[str, dict, list[dict]]]:
    """Regroupe des fiches par document physique.

    Rend une liste de (cle, canonique, doublons). Une fiche sans cle
    exploitable forme son PROPRE groupe : dans le doute, on ne fusionne
    pas. L'ordre d'entree est preserve pour que le resultat soit
    reproductible.
    """
    groupes: dict[str, list[dict]] = {}
    ordre: list[str] = []
    for index, fiche in enumerate(fiches):
        cle = business_document_key(fiche)
        if not cle:
            # Cle propre, impossible a collisionner avec une autre fiche.
            cle = f"isolee:{index}:{fiche.get('doc_key') or index}"
        if cle not in groupes:
            groupes[cle] = []
            ordre.append(cle)
        groupes[cle].append(fiche)

    resultat: list[tuple[str, dict, list[dict]]] = []
    for cle in ordre:
        membres = groupes[cle]
        classees = sorted(membres, key=completeness, reverse=True)
        canonique = classees[0]
        doublons = [f for f in membres if f is not canonique]
        resultat.append((cle, canonique, doublons))
    return resultat
