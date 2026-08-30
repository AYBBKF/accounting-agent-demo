"""Routage d'un email entrant vers UNE entreprise, ou vers la quarantaine.

Le routage est la frontiere de securite du multi-tenant : c'est lui qui
decide dans quelle comptabilite une piece va entrer. Il est donc conçu
pour REFUSER par defaut.

Ordre de decision, strict :

  1. `Delivered-To` / `X-Original-To` - l'adresse a laquelle le serveur a
     REELLEMENT livre le message. C'est la seule que l'expediteur ne
     controle pas : elle est posee par Gmail a la livraison.
  2. Adresse exacte presente dans `To` / `Cc`. Controlee par
     l'expediteur, donc utilisee seulement si l'etape 1 n'a rien donne.
  3. Tag `[ACCOUNTING:<company_id>]` dans le sujet, et UNIQUEMENT si
     l'expediteur figure dans les administrateurs autorises de cette
     entreprise. Sans cette condition, n'importe qui router ait une
     facture dans n'importe quelle comptabilite en ecrivant un sujet.

Un nom d'entreprise ecrit en clair dans le sujet ou le corps ne route
JAMAIS. La correspondance approximative de nom n'existe pas ici : deux
societes peuvent s'appeler « Flux » et « Flux Intelligent », et une
erreur de rapprochement melangerait deux comptabilites.

Toute ambiguite - deux entreprises designees, un tag qui contredit
l'alias - conduit a la quarantaine de routage : zero ecriture, zero
creation d'entreprise, conservation de la piece et alerte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Any, Iterable

from app import companies as registry

# --- issues du routage ----------------------------------------------------

ROUTED = "routed"                       # entreprise trouvee et ecrivable
NOT_WRITABLE = "not_writable"           # trouvee, mais pas en etat d'ecrire
UNKNOWN_COMPANY = "unknown_company"     # aucun alias connu
CONFLICT = "conflict"                   # alias et tag se contredisent
AMBIGUOUS = "ambiguous"                 # plusieurs entreprises designees

# Seule cette issue autorise la suite du traitement comptable.
ACCEPTED = frozenset({ROUTED})

# Sources de decision, de la plus fiable a la moins fiable.
SOURCE_DELIVERED_TO = "delivered_to"
SOURCE_RECIPIENT = "recipient"
SOURCE_ADMIN_TAG = "admin_tag"

_DELIVERY_HEADERS = ("delivered-to", "x-original-to")
_RECIPIENT_HEADERS = ("to", "cc")

# Tag administrateur. Volontairement rigide : un identifiant d'entreprise
# entre crochets, rien d'autre. Pas de nom libre, pas d'espace tolere au
# milieu, pas de casse variable sur la structure.
_ADMIN_TAG_RE = re.compile(r"\[ACCOUNTING:([a-z0-9-]{2,63})\]", re.IGNORECASE)


@dataclass(frozen=True)
class RoutingDecision:
    """Ce que le routage a decide, et pourquoi.

    `reason` est destine a l'humain qui lira la quarantaine : il doit
    suffire a comprendre le refus sans rouvrir les journaux.
    """

    outcome: str
    company_id: str = ""
    source: str = ""
    reason: str = ""
    candidates: tuple[str, ...] = ()
    delivered_to: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.outcome in ACCEPTED

    @property
    def quarantined(self) -> bool:
        return self.outcome in (UNKNOWN_COMPANY, CONFLICT, AMBIGUOUS)


def _headers_map(message: dict[str, Any]) -> dict[str, list[str]]:
    """En-tetes reels du message, en minuscules, valeurs groupees.

    Gmail peut repeter `Delivered-To` : on garde TOUTES les valeurs, car
    c'est justement la repetition qui revele un transfert entre alias.
    """
    brut: dict[str, list[str]] = {}
    payload = message.get("payload") or {}
    for entree in payload.get("headers") or ():
        nom = str(entree.get("name") or "").strip().lower()
        valeur = str(entree.get("value") or "").strip()
        if nom and valeur:
            brut.setdefault(nom, []).append(valeur)
    return brut


def _addresses(valeurs: Iterable[str]) -> tuple[str, ...]:
    """Adresses normalisees extraites d'en-tetes potentiellement composes.

    `getaddresses` gere « Nom <adresse> », les listes separees par des
    virgules et les guillemets - autant de formes qu'un decoupage naif
    sur la virgule casserait.
    """
    paires = getaddresses([v for v in valeurs if v])
    sorties: list[str] = []
    for _nom, adresse in paires:
        propre = registry.normalize_alias(adresse)
        if propre and propre not in sorties:
            sorties.append(propre)
    return tuple(sorties)


def _companies_for(db_path: str, adresses: Iterable[str]) -> tuple[str, ...]:
    trouvees: list[str] = []
    for adresse in adresses:
        entreprise = registry.company_for_alias(db_path, adresse)
        if entreprise is not None and entreprise.company_id not in trouvees:
            trouvees.append(entreprise.company_id)
    return tuple(trouvees)


def _admin_tag(subject: str) -> str:
    """Identifiant demande par un tag administrateur, ou chaine vide.

    Deux tags differents dans un meme sujet sont traites comme une
    absence de tag exploitable : on ne choisit pas le premier venu.
    """
    trouves = {m.group(1).lower() for m in _ADMIN_TAG_RE.finditer(subject or "")}
    return trouves.pop() if len(trouves) == 1 else ""


def _sender(message: dict[str, Any], entetes: dict[str, list[str]]) -> str:
    brut = entetes.get("from") or [str(message.get("sender") or "")]
    adresses = _addresses(brut)
    return adresses[0] if adresses else ""


def route_message(db_path: str, message: dict[str, Any]) -> RoutingDecision:
    """Decide l'entreprise destinataire d'UN email, ou refuse."""
    entetes = _headers_map(message)

    livraison = _addresses(
        valeur
        for nom in _DELIVERY_HEADERS
        for valeur in entetes.get(nom, ())
    )
    destinataires = _addresses(
        valeur
        for nom in _RECIPIENT_HEADERS
        for valeur in entetes.get(nom, ())
    )
    if not destinataires and message.get("to"):
        destinataires = _addresses([str(message.get("to"))])

    sujet = str(
        entetes.get("subject", [message.get("subject") or ""])[0]
    )
    expediteur = _sender(message, entetes)

    par_livraison = _companies_for(db_path, livraison)
    par_destinataire = _companies_for(db_path, destinataires)

    # Une meme livraison ne peut pas designer deux comptabilites.
    if len(par_livraison) > 1:
        return RoutingDecision(
            outcome=AMBIGUOUS,
            candidates=par_livraison,
            delivered_to=livraison,
            reason=(
                "plusieurs entreprises sont designees par les en-tetes de "
                f"livraison : {', '.join(par_livraison)}"
            ),
        )

    retenue = par_livraison[0] if par_livraison else ""
    source = SOURCE_DELIVERED_TO if retenue else ""

    if not retenue:
        if len(par_destinataire) > 1:
            return RoutingDecision(
                outcome=AMBIGUOUS,
                candidates=par_destinataire,
                delivered_to=livraison,
                reason=(
                    "plusieurs entreprises sont designees dans To/Cc : "
                    f"{', '.join(par_destinataire)}"
                ),
            )
        if par_destinataire:
            retenue = par_destinataire[0]
            source = SOURCE_RECIPIENT

    tag = _admin_tag(sujet)

    # Le tag ne vaut que s'il designe une entreprise connue ET que
    # l'expediteur est administrateur DE CETTE entreprise.
    tag_autorise = ""
    if tag:
        cible = registry.get_company(db_path, tag)
        if cible is not None and expediteur:
            admins = {
                registry.normalize_alias(a) for a in cible.allowed_admin_senders
            }
            if expediteur in admins:
                tag_autorise = cible.company_id

    if retenue and tag_autorise and tag_autorise != retenue:
        return RoutingDecision(
            outcome=CONFLICT,
            candidates=(retenue, tag_autorise),
            delivered_to=livraison,
            reason=(
                f"l'adresse de reception designe '{retenue}' alors que le tag "
                f"administrateur designe '{tag_autorise}'"
            ),
        )

    if not retenue and tag_autorise:
        retenue, source = tag_autorise, SOURCE_ADMIN_TAG

    if not retenue:
        # Un tag present mais non autorise ne doit pas passer pour une
        # simple absence de destinataire : le motif le dit, sans quoi
        # l'exploitant chercherait une erreur d'alias inexistante.
        motif = "aucun alias enregistre ne correspond a cet email"
        if tag:
            motif += (
                f" ; le tag '[ACCOUNTING:{tag}]' a ete ignore "
                "(expediteur non administrateur ou entreprise inconnue)"
            )
        return RoutingDecision(
            outcome=UNKNOWN_COMPANY,
            delivered_to=livraison,
            reason=motif,
        )

    entreprise = registry.get_company(db_path, retenue)
    if entreprise is None:
        return RoutingDecision(
            outcome=UNKNOWN_COMPANY,
            delivered_to=livraison,
            reason=f"entreprise '{retenue}' absente du registre",
        )

    if not entreprise.can_write:
        manquants = entreprise.missing_for_activation
        detail = f"statut {entreprise.status}"
        if manquants:
            detail += f", configuration incomplete : {', '.join(manquants)}"
        return RoutingDecision(
            outcome=NOT_WRITABLE,
            company_id=entreprise.company_id,
            source=source,
            delivered_to=livraison,
            reason=f"l'entreprise '{entreprise.company_id}' ne peut pas ecrire ({detail})",
        )

    return RoutingDecision(
        outcome=ROUTED,
        company_id=entreprise.company_id,
        source=source,
        delivered_to=livraison,
        reason="",
    )
