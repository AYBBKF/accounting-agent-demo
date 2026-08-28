"""Bascule d'execution : mono-entreprise hier, multi-entreprises demain.

Le code multi-tenant existe et est teste, mais tant que le processus
n'appelle pas le repartiteur, il ne sert a rien en production. Ce module
est ce branchement - et il est ecrit pour que l'ancien comportement reste
atteignable a tout moment.

Deux principes.

  * LE MULTI-TENANT S'ACTIVE EXPLICITEMENT. Sans `MULTI_TENANT_ENABLED`,
    le processus se comporte exactement comme avant : meme worker, meme
    classeur, meme requete. Un deploiement qui echouerait a lire sa
    configuration retombe donc sur un mode connu, pas sur un mode
    degrade inconnu.

  * UNE ENTREPRISE N'EXISTE QUE PAR DECISION D'ADMINISTRATEUR. Les
    entreprises se declarent dans `COMPANIES_JSON`, une variable posee a
    la main par l'exploitant. Aucun email, aucun nom de fichier, aucun
    sujet ne peut faire naitre une entreprise. Chaque declaration est
    journalisee au demarrage, ce qui la rend tracable apres coup.

L'ordre de demarrage compte et n'est pas negociable :

    1. migration de l'etat legacy vers `xblaste` ;
    2. declaration des entreprises au registre ;
    3. bootstrap de celles qui n'ont pas encore leur classeur ;
    4. seulement ensuite, ouverture du repartiteur.

Une entreprise dont le bootstrap echoue reste `PENDING_CONFIGURATION` :
elle ne traitera rien, et les autres continuent sans elle.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app import companies as registry
from app import llm_usage
from app import tenancy
from app.tenant_worker import TenantWorker

logger = logging.getLogger(__name__)


class RuntimeConfigError(RuntimeError):
    """Configuration multi-tenant inexploitable : on refuse de demarrer.

    Demarrer a moitie configure serait pire que ne pas demarrer : les
    emails d'un client seraient vus mais jamais traites, sans erreur
    visible.
    """


# Champs qu'une declaration d'entreprise peut porter. Tout autre champ
# est refuse : une faute de frappe silencieuse produirait une entreprise
# incomplete qu'on croirait complete.
_CHAMPS = {
    "company_id", "display_name", "legal_name", "ice", "country", "currency",
    "allowed_vat_rates", "telegram_chat_id", "inbound_aliases", "sheet_id",
    "drive_folder_id", "status", "allowed_admin_senders",
}
_OBLIGATOIRES = ("company_id", "inbound_aliases")


@dataclass
class StartupReport:
    """Ce que le demarrage a REELLEMENT fait, entreprise par entreprise."""

    migrated: bool = False
    declared: tuple[str, ...] = ()
    activated: tuple[str, ...] = ()
    pending: dict[str, str] = field(default_factory=dict)
    writable: tuple[str, ...] = ()


def parse_companies(brut: str) -> list[dict[str, Any]]:
    """Lit la declaration d'entreprises, ou echoue franchement.

    Une declaration illisible ne doit pas etre ignoree en silence : elle
    signifie que l'exploitant croit avoir configure des entreprises qui
    n'existent pas.
    """
    texte = str(brut or "").strip()
    if not texte:
        return []
    try:
        charge = json.loads(texte)
    except json.JSONDecodeError as exc:
        raise RuntimeConfigError(
            f"COMPANIES_JSON illisible : {exc.msg} (ligne {exc.lineno})"
        ) from exc
    if isinstance(charge, dict):
        charge = [charge]
    if not isinstance(charge, list):
        raise RuntimeConfigError("COMPANIES_JSON doit etre une liste d'entreprises")

    sorties: list[dict[str, Any]] = []
    for entree in charge:
        if not isinstance(entree, dict):
            raise RuntimeConfigError("chaque entreprise doit etre un objet JSON")
        inconnus = set(entree) - _CHAMPS
        if inconnus:
            raise RuntimeConfigError(
                f"champs inconnus dans COMPANIES_JSON : {', '.join(sorted(inconnus))}"
            )
        for champ in _OBLIGATOIRES:
            if not entree.get(champ):
                raise RuntimeConfigError(
                    f"champ obligatoire manquant dans COMPANIES_JSON : {champ}"
                )
        sorties.append(dict(entree))
    return sorties


def declare_companies(db_path: str, declarations: list[dict[str, Any]]) -> list[str]:
    """Inscrit ou met a jour les entreprises declarees par l'exploitant.

    Une entreprise deja connue n'est PAS recreee : ses identifiants de
    classeur et de dossier, acquis au bootstrap, doivent survivre a un
    redemarrage.
    """
    registry.ensure_schema(db_path)
    inscrites: list[str] = []
    for declaration in declarations:
        identifiant = registry.normalize_company_id(str(declaration["company_id"]))
        existante = registry.get_company(db_path, identifiant)
        champs = {k: v for k, v in declaration.items() if k != "company_id"}
        if existante is None:
            registry.register_company(db_path, identifiant, **champs)
            logger.info(
                "Entreprise declaree par l'exploitant : %s (alias : %s)",
                identifiant, ", ".join(declaration.get("inbound_aliases") or []),
            )
        else:
            # `status`, les alias et la date de creation ne se modifient
            # pas par simple redeclaration : ce sont des decisions, pas
            # de la configuration.
            modifiables = {
                k: v for k, v in champs.items()
                if k not in ("status", "inbound_aliases")
            }
            if modifiables:
                registry.update_company(db_path, identifiant, **modifiables)
            logger.info("Entreprise deja connue : %s (mise a jour)", identifiant)
        inscrites.append(identifiant)
    return inscrites


def bootstrap_pending(
    db_path: str, identifiants: list[str], *, sheets: Any, drive: Any,
    template_sheet_id: str,
) -> tuple[list[str], dict[str, str]]:
    """Bootstrape ce qui peut l'etre, sans bloquer les autres.

    L'echec d'une entreprise est un incident local : les comptabilites
    voisines n'ont aucune raison d'attendre.
    """
    from app import bootstrap as amorce

    activees: list[str] = []
    en_attente: dict[str, str] = {}
    for identifiant in identifiants:
        entreprise = registry.get_company(db_path, identifiant)
        if entreprise is None:
            en_attente[identifiant] = "inconnue du registre"
            continue
        if entreprise.can_write:
            activees.append(identifiant)
            continue
        try:
            rapport = amorce.bootstrap_company(
                db_path, identifiant, sheets=sheets, drive=drive,
                template_sheet_id=template_sheet_id,
            )
        except Exception as exc:  # noqa: BLE001 - un tenant n'en bloque pas d'autres
            en_attente[identifiant] = f"{type(exc).__name__}: {exc}"
            logger.warning("Bootstrap refuse pour %s : %s", identifiant, exc)
            continue
        if getattr(rapport, "activated", False):
            activees.append(identifiant)
            logger.info("Entreprise activee : %s", identifiant)
        else:
            raison = ", ".join(
                tuple(getattr(rapport, "blocked_by", ()) or ())
                + tuple(getattr(rapport, "missing_tabs", ()) or ())
            ) or "bootstrap incomplet"
            en_attente[identifiant] = raison
            logger.warning(
                "Entreprise laissee en attente de configuration : %s (%s)",
                identifiant, raison,
            )
    return activees, en_attente


def prepare(
    db_path: str, *, companies_json: str, sheets: Any = None, drive: Any = None,
    template_sheet_id: str = "",
) -> StartupReport:
    """Prepare l'etat multi-tenant AVANT d'ouvrir le repartiteur."""
    rapport = StartupReport()

    migration = tenancy.migrate_to_multi_tenant(db_path)
    rapport.migrated = not migration.already_migrated
    if rapport.migrated:
        logger.info(
            "Migration multi-tenant : %s ligne(s) rattachee(s) a %s, "
            "sauvegardes conservees : %s",
            migration.total_rows, migration.company_id,
            ", ".join(migration.legacy_tables_kept) or "aucune",
        )
    llm_usage.ensure_schema(db_path)

    declarations = parse_companies(companies_json)
    rapport.declared = tuple(declare_companies(db_path, declarations))

    if sheets is not None and drive is not None and template_sheet_id:
        activees, en_attente = bootstrap_pending(
            db_path, list(rapport.declared), sheets=sheets, drive=drive,
            template_sheet_id=template_sheet_id,
        )
        rapport.activated = tuple(activees)
        rapport.pending = en_attente
    else:
        logger.info(
            "Bootstrap non tente : aucun classeur modele configure "
            "(TEMPLATE_SHEET_ID). Les entreprises deja completes restent actives."
        )

    rapport.writable = tuple(
        c.company_id for c in registry.list_companies(db_path) if c.can_write
    )
    logger.info(
        "Etat multi-tenant : %d declaree(s), %d ecrivable(s) : %s",
        len(rapport.declared), len(rapport.writable),
        ", ".join(rapport.writable) or "aucune",
    )
    for identifiant, raison in rapport.pending.items():
        logger.warning("En attente de configuration : %s (%s)", identifiant, raison)
    return rapport


def build_worker(settings: Any, **extra: Any) -> TenantWorker:
    """Construit le repartiteur a partir de la configuration de l'exploitant.

    La requete Gmail est passee TELLE QUELLE : le repartiteur refuse de
    demarrer si elle est vide, plutot que de retomber sur une requete
    plus large qui ferait entrer des emails de test en comptabilite.
    """
    return TenantWorker(
        api_key=settings.composio_api_key,
        chat_id=settings.gmail_watch_chat_id,
        db_path=settings.db_path,
        query=settings.gmail_watch_query,
        poll_seconds=settings.gmail_watch_interval_seconds,
        max_per_cycle=settings.gmail_watch_max_per_cycle,
        zip_limits=settings.zip_limits(),
        **extra,
    )
