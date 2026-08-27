"""Nettoyage AUDITE de la production. Prepare, jamais declenche seul.

Ce module corrige les degats constates dans le classeur du 26 aout 2026 :

  A. trois ecritures comptables qui n'auraient jamais du etre creees -
     une facture datee de 2027, une TVA a 17 %, un montant negatif sur
     une facture d'achat ;
  B. onze documents physiques inscrits DEUX FOIS dans `21_A_VERIFIER`,
     une fois depuis le ZIP et une fois depuis l'envoi separe.

Quatre principes gouvernent tout ce qui suit, et aucun n'est negociable :

  1. RIEN n'est supprime definitivement. Une ecriture fautive est
     ANNULEE - ses montants quittent les totaux, sa ligne reste, avec son
     identifiant et son motif. Une entree de `14_IMPORTS_LOG` est
     MARQUEE, jamais retiree : c'est le journal d'audit.
  2. AUCUNE renumerotation. `FA-2026-034` reste le dernier identifiant
     attribue ; les identifiants annules ne sont ni reattribues, ni
     combles.
  3. Rien ne s'execute sans deux sauvegardes VERIFIEES : la base SQLite
     (taille, SHA-256, `integrity_check=ok`) et une copie complete de
     `21_A_VERIFIER`, relue et comparee.
  4. `plan()` ne modifie RIEN. C'est lui qu'on regarde avant de decider,
     et c'est lui qui produit la liste exacte soumise a validation.

Ce module n'est volontairement PAS enregistre parmi les taches de
demarrage : deployer l'image ne declenche aucun nettoyage. Il faut un
appel explicite a `execute()`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app import doc_store as store
from app.business_key import group_by_business_key
from app.db_backup import BackupError, verified_backup
from app.review_sheet import TAB_REVIEW

logger = logging.getLogger("demo_bot.cleanup")

# Onglet ou l'on conserve l'image AVANT annulation de chaque ligne touchee.
# Sans elle, "annuler" reviendrait a effacer : on garde de quoi remonter.
TAB_CANCELLATIONS = "22_JOURNAL_ANNULATIONS"
CANCELLATION_HEADERS = [
    "Horodatage", "Onglet", "Identifiant", "Motif",
    "Contenu avant annulation (JSON)",
]

TAB_ACCOUNTING = "05_FACTURES_ACHATS"
TAB_IMPORTS = "14_IMPORTS_LOG"
# Onglets dependants : une ecriture annulee doit l'etre partout ou elle a
# laisse une trace chiffree.
DEPENDENT_TABS = ("12_JOURNAL_COMPTABLE", "16_LIGNES_FACTURES", "19_ECHEANCES_A_PAYER")

# TOUS les onglets que le nettoyage peut modifier. Sauvegarder le seul
# 21_A_VERIFIER laissait 05_FACTURES_ACHATS, 16_LIGNES_FACTURES et
# 14_IMPORTS_LOG sans filet : le rollback etait incomplet par
# construction. On copie chacun, et on relit chaque copie.
MODIFIED_TABS = (TAB_ACCOUNTING, TAB_IMPORTS, TAB_REVIEW, *DEPENDENT_TABS)

STATUS_CANCELLED = "Annulee apres controle"
STATUS_SUPERSEDED = "Superseded"

# Les trois ecritures fautives, par NUMERO DE DOCUMENT et non par position :
# une suppression anterieure deplacerait les lignes.
WRONG_ENTRIES = (
    ("FAC-ACH-2026-511", "document date dans le futur (2027-01-15)"),
    ("FAC-ACH-2026-512", "taux de TVA 17% absent des taux autorises (0, 7, 10, 20)"),
    ("FAC-ACH-2026-513", "montant negatif sur un document qui n'est pas un avoir"),
)


class CleanupAborted(RuntimeError):
    """Le nettoyage s'est arrete AVANT d'ecrire quoi que ce soit."""


@dataclass
class Plan:
    """Ce qui SERAIT fait. Produit sans rien modifier."""

    cancellations: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    imports_to_mark: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.cancellations or self.duplicates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "annulations": self.cancellations,
            "doublons": self.duplicates,
            "journal_a_marquer": self.imports_to_mark,
            "avertissements": self.warnings,
        }


def plan(pipeline, db_path: str, chat_id: int) -> Plan:
    """Etablit la liste exacte des corrections. N'ECRIT RIEN.

    Chaque element est identifie par sa reference documentaire ET son
    identifiant metier - jamais par un numero de ligne seul, puisque toute
    suppression deplacerait les positions.
    """
    resultat = Plan()
    lignes = pipeline.read_tab(TAB_ACCOUNTING)
    entetes, corps = (lignes[0], lignes[1:]) if lignes else ([], [])

    for numero, motif in WRONG_ENTRIES:
        trouvees = [
            (index, ligne) for index, ligne in enumerate(corps, start=2)
            if len(ligne) > 2 and str(ligne[2]).strip() == numero
        ]
        if not trouvees:
            resultat.warnings.append(
                f"{numero} : aucune ligne trouvee dans {TAB_ACCOUNTING} "
                f"(deja corrigee ?) - rien ne sera fait"
            )
            continue
        if len(trouvees) > 1:
            resultat.warnings.append(
                f"{numero} : {len(trouvees)} lignes portent ce numero - "
                f"verification humaine requise AVANT execution"
            )
        for index, ligne in trouvees:
            if any("ANNULEE APRES CONTROLE" in str(c) for c in ligne):
                # Deja annulee lors d'un passage precedent. La re-annuler
                # ajouterait une seconde image "avant" au journal - une
                # image du document DEJA vide, donc inutilisable pour un
                # rollback. Un second execute() ne doit rien changer.
                resultat.warnings.append(
                    f"{numero} : deja annulee, laissee en l'etat"
                )
                continue
            resultat.cancellations.append({
                "numero": numero,
                "identifiant": str(ligne[0]) if ligne else "",
                "ligne_indicative": index,
                "motif": motif,
                "avant": list(ligne),
            })

    for fiche, canonique in _duplicate_pairs(db_path, chat_id):
        resultat.duplicates.append({
            "doc_key": str(fiche["doc_key"]),
            "canonique": str(canonique["doc_key"]),
            "numero": str(fiche.get("numero") or ""),
            "type": str(fiche.get("doc_type") or ""),
            "ligne_indicative": int(fiche.get("review_row") or 0),
        })
        resultat.imports_to_mark.append({
            "doc_key": str(fiche["doc_key"]),
            "statut": STATUS_SUPERSEDED,
            "canonique": str(canonique["doc_key"]),
        })

    for annulation in resultat.cancellations:
        resultat.imports_to_mark.append({
            "identifiant": annulation["identifiant"],
            "statut": STATUS_CANCELLED,
            "canonique": "",
        })
    return resultat


def _duplicate_pairs(db_path: str, chat_id: int) -> list[tuple[dict, dict]]:
    """(fiche secondaire, fiche canonique) pour chaque doublon physique.

    Le regroupement passe par la cle metier, jamais par le `doc_key` :
    celui-ci contient l'identifiant du message Gmail, donc deux exemplaires
    du meme document n'ont jamais le meme.
    """
    fiches = [
        f for f in store.list_quarantined(db_path, chat_id)
        if int(f.get("review_row") or 0)
    ]
    paires: list[tuple[dict, dict]] = []
    for _, _propose, _doublons in group_by_business_key(fiches):
        membres = [_propose, *_doublons]
        # La canonique est la ligne DEJA EN PLACE la plus haute dans
        # l'onglet. Ce choix n'est pas cosmetique : garder la ligne
        # existante evite de la deplacer, donc de perimer les references
        # que le comptable a pu noter ailleurs.
        canonique = min(
            membres,
            key=lambda f: (int(f.get("review_row") or 10**9), str(f.get("created_at") or "")),
        )
        for doublon in membres:
            if doublon is canonique:
                continue
            paires.append((doublon, canonique))
    return paires


def execute(
    pipeline, db_path: str, chat_id: int, *, confirmed: bool = False
) -> dict[str, Any]:
    """Applique le plan, apres deux sauvegardes VERIFIEES.

    `confirmed` doit valoir True explicitement. Ce garde-fou n'est pas
    decoratif : ce module est importable, et un appel distrait ne doit pas
    pouvoir modifier une comptabilite.
    """
    if not confirmed:
        raise CleanupAborted(
            "execute() exige confirmed=True : aucun nettoyage n'a eu lieu"
        )

    # --- filet 1 : la base ------------------------------------------------
    try:
        sauvegarde = verified_backup(db_path, "nettoyage")
    except BackupError as exc:
        logger.error("Nettoyage interrompu : sauvegarde SQLite refusee (%s)", exc)
        raise CleanupAborted(f"sauvegarde SQLite non verifiee : {exc}") from exc
    logger.info(
        "Sauvegarde avant nettoyage | chemin=%s | taille=%s octets | "
        "sha256=%s | integrity_check=%s",
        sauvegarde["path"], sauvegarde["size"], sauvegarde["sha256"],
        sauvegarde["integrity_check"],
    )

    # --- filet 2 : une copie relue de CHAQUE onglet modifie ----------------
    copies: dict[str, str] = {}
    try:
        for onglet in MODIFIED_TABS:
            nom = pipeline.backup_tab(onglet)
            if nom:
                copies[onglet] = nom
    except Exception as exc:  # noqa: BLE001 - une copie douteuse arrete tout
        logger.error("Nettoyage interrompu : copie d'onglet refusee (%s)", exc)
        raise CleanupAborted(f"copie d'onglet non verifiee : {exc}") from exc

    if TAB_REVIEW in pipeline.tabs() and TAB_REVIEW not in copies:
        raise CleanupAborted(
            f"copie de {TAB_REVIEW} non confirmee : aucune ligne modifiee"
        )
    logger.info(
        "Copies verifiees avant nettoyage : %s",
        ", ".join(f"{k} -> {v}" for k, v in copies.items()) or "(aucune)",
    )

    projet = plan(pipeline, db_path, chat_id)
    pipeline.ensure_tab_with_headers(TAB_CANCELLATIONS, CANCELLATION_HEADERS)

    annulees = 0
    for annulation in projet.cancellations:
        _cancel_entry(pipeline, db_path, annulation)
        annulees += 1

    marquees = _mark_import_log(pipeline, projet)
    rattachees = _detach_duplicates(db_path, projet)
    reconstruites = rebuild_review_tab(pipeline, db_path, chat_id)

    logger.info(
        "Nettoyage termine | %d ecriture(s) annulee(s) | %d doublon(s) "
        "rattache(s) | %d entree(s) de journal marquee(s) | "
        "%d ligne(s) de quarantaine apres reconstruction",
        annulees, rattachees, marquees, reconstruites,
    )
    return {
        "backup_sqlite": sauvegarde,
        "backups": copies,
        "backup_review_tab": copies.get(TAB_REVIEW, ""),
        "cancelled": annulees,
        "duplicates_detached": rattachees,
        "import_log_marked": marquees,
        "review_rows_after": reconstruites,
        "warnings": projet.warnings,
    }


def rebuild_review_tab(pipeline, db_path: str, chat_id: int) -> int:
    """Reecrit `21_A_VERIFIER` a partir des SEULES fiches canoniques.

    Le defaut corrige ici : marquer un doublon en base ne retire pas sa
    ligne de l'onglet. `execute()` annoncait `duplicates_detached=1` et le
    classeur gardait ses deux lignes.

    Trois consequences, et la troisieme est celle qu'on oublie :
      - les lignes secondaires disparaissent de l'onglet ;
      - les canoniques sont reecrites dans l'ordre, sans trou ;
      - les `review_row` memorises en base sont RECALCULES, parce qu'un
        compactage deplace les lignes et perime toutes les positions
        connues.

    Rend le nombre de lignes de donnees apres reconstruction.
    """
    canoniques = [
        fiche for fiche in store.list_quarantined(db_path, chat_id)
        if not str(fiche.get("superseded_by") or "")
    ]
    entrees = [_entry_of(fiche) for fiche in canoniques]
    positions = pipeline.rewrite_review_rows(entrees)

    for fiche, ligne in zip(canoniques, positions):
        store.update_document(db_path, fiche["doc_key"], review_row=ligne)

    # Relecture par le MEME chemin qu'un consommateur quelconque : c'est
    # ce que verra le comptable, pas l'etat interne de la grille.
    relu = pipeline.read_tab(TAB_REVIEW)
    corps = [ligne for ligne in relu[1:] if any(str(c).strip() for c in ligne)]
    reelles = len(corps)
    if reelles != len(entrees):
        raise CleanupAborted(
            f"{TAB_REVIEW} reconstruit a {reelles} ligne(s) au lieu de "
            f"{len(entrees)} : restauration necessaire"
        )
    logger.info(
        "%s reconstruit et relu : %d ligne(s) canonique(s), review_row "
        "recalcules", TAB_REVIEW, reelles,
    )
    return reelles


def _entry_of(fiche: dict[str, Any]) -> "ReviewEntry":
    """Reconstruit la ligne de quarantaine d'une fiche depuis la base.

    Les motifs vivent dans `payload`, ecrit au moment de la mise en
    quarantaine. On ne les REINVENTE pas : si le payload est illisible, on
    le dit dans la ligne plutot que de fabriquer une explication.
    """
    from app.review_sheet import ReviewEntry

    try:
        charge = json.loads(str(fiche.get("payload") or "{}"))
        motifs = [str(m) for m in charge.get("reasons", [])]
    except (ValueError, TypeError):
        motifs = []
    return ReviewEntry(
        doc_key=str(fiche["doc_key"]),
        detected_at=str(fiche.get("created_at") or ""),
        type_label=str(fiche.get("doc_type") or ""),
        numero=str(fiche.get("numero") or ""),
        tiers="",
        devise="",
        reasons=motifs or ["motif d'origine non relu depuis la base"],
        drive_link=str(fiche.get("drive_link") or ""),
        gmail_message_id=str(fiche.get("gmail_message_id") or ""),
        filename=str(fiche.get("filename") or ""),
    )


def rollback(pipeline, backups: dict[str, str]) -> dict[str, int]:
    """Restaure chaque onglet depuis sa copie verifiee.

    Contrepartie exacte de `execute()`. Elle existe pour etre APPELEE, pas
    pour figurer dans un plan : un rollback qu'on n'a jamais execute n'est
    pas un rollback.
    """
    restaures: dict[str, int] = {}
    for onglet, copie in backups.items():
        restaures[onglet] = pipeline.restore_tab(onglet, copie)
    logger.info(
        "Rollback effectue : %s",
        ", ".join(f"{k} ({v} lignes)" for k, v in restaures.items()),
    )
    return restaures


def _cancel_entry(pipeline, db_path: str, annulation: dict[str, Any]) -> None:
    """Annule UNE ecriture : image avant, montants neutralises, motif.

    La ligne n'est PAS supprimee. La supprimer decalerait toutes les
    suivantes et ferait mentir les references deja notees ailleurs ; et
    surtout, une comptabilite ne s'efface pas, elle se contre-passe.
    """
    identifiant = annulation["identifiant"]
    pipeline.append_cancellation(
        TAB_ACCOUNTING, identifiant, annulation["motif"],
        json.dumps(annulation["avant"], ensure_ascii=False, default=str),
    )
    pipeline.neutralize_amounts(TAB_ACCOUNTING, identifiant, annulation["motif"])
    for onglet in DEPENDENT_TABS:
        touchees = pipeline.neutralize_amounts(onglet, identifiant, annulation["motif"])
        if touchees:
            logger.info(
                "%s : %d ligne(s) dependante(s) de %s annulee(s)",
                onglet, touchees, identifiant,
            )
    fiche = store.find_by_stable_id(db_path, identifiant)
    if fiche:
        store.update_document(
            db_path, fiche["doc_key"],
            state=store.NEEDS_REVIEW,
            payload=json.dumps(
                {"reasons": [annulation["motif"]]}, ensure_ascii=False
            ),
        )


def _mark_import_log(pipeline, projet: Plan) -> int:
    """Marque le journal d'import SANS jamais en retirer une ligne."""
    marquees = 0
    for entree in projet.imports_to_mark:
        reference = entree.get("identifiant") or entree.get("doc_key") or ""
        if not reference:
            continue
        suffixe = (
            f" ({entree['canonique'][:12]})" if entree.get("canonique") else ""
        )
        if pipeline.mark_import_log(reference, f"{entree['statut']}{suffixe}"):
            marquees += 1
    return marquees


def _detach_duplicates(db_path: str, projet: Plan) -> int:
    """Rattache les fiches secondaires. Aucune n'est supprimee."""
    rattachees = 0
    for doublon in projet.duplicates:
        store.update_document(
            db_path, doublon["doc_key"],
            superseded_by=doublon["canonique"],
            state=store.SUPERSEDED,
            review_row=0,
        )
        rattachees += 1
    return rattachees
