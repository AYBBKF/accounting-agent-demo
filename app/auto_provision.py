"""Naissance d'une entreprise a partir d'une adresse de reception.

Jusqu'ici, une entreprise n'existait que par decision d'administrateur
(`COMPANIES_JSON`). L'exploitant a demande l'inverse pour ses adresses
sous-adressees : un email livre a `base+argana78@gmail.com` doit creer la
comptabilite `argana78` - classeur, dossier Drive, registre - puis la
servir immediatement.

Ce module isole ce chemin, parce qu'il ouvre une porte que le reste de
l'agent tient fermee. Il l'ouvre le moins possible :

  * SEULE L'ADRESSE DE RECEPTION FAIT NAITRE. Le sujet, le corps, le nom
    d'un fichier ou l'expediteur ne creent jamais rien. La partie locale
    avant le `+` doit etre EXACTEMENT celle de la boite de l'exploitant :
    un email adresse a `autrechose+x@gmail.com` ne provisionne rien.
  * L'IDENTIFIANT EST CONTRAINT. Minuscules, chiffres et tirets, 2 a 40
    caracteres. Un tag qui ne respecte pas cette forme est refuse plutot
    que « nettoye » : on ne devine pas le nom d'une comptabilite.
  * L'IDENTITE FISCALE N'EST JAMAIS INVENTEE. La raison sociale vaut
    l'identifiant, l'ICE reste VIDE. Un ICE absent desactive simplement
    le controle d'orientation par ICE ; il ne fabrique pas une identite
    legale que personne n'a declaree.
  * UNE CREATION PAR EMAIL, ET UN PLAFOND. Le plafond protege le Drive de
    l'exploitant d'une avalanche d'adresses inventees : au-dela, plus
    aucune entreprise ne nait et le refus est journalise.
  * LES ENTREPRISES DECLAREES RESTENT PRIORITAIRES. Si l'identifiant
    existe deja au registre, ce module ne touche a rien : c'est le
    routage normal qui decide.

Une entreprise nee ici est ordinaire : meme registre, meme bootstrap,
meme isolation. Rien de ce qui suit ne connait son origine.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app import bootstrap as amorce
from app import companies as registry
from app import routing

logger = logging.getLogger(__name__)

# Forme admise pour un identifiant d'entreprise ne d'une adresse. Volontairement
# plus stricte que le registre : ce qui naitra tout seul doit etre lisible
# par un humain dans un Drive.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")

# Tags qui ne peuvent JAMAIS faire naitre une entreprise : ils designent
# des usages internes de la boite.
RESERVED_SLUGS = frozenset({
    "xblaste", "test", "tests", "verif", "verification", "e2e", "demo",
    "admin", "root", "noreply", "no-reply", "postmaster", "abuse",
})


class ProvisionRefused(RuntimeError):
    """L'adresse ne peut pas faire naitre d'entreprise, et on dit pourquoi."""


@dataclass(frozen=True)
class ProvisionDefaults:
    """Ce qu'une entreprise nee d'une adresse recoit, faute de declaration.

    Aucune de ces valeurs n'est une donnee legale : ce sont les reglages
    d'exploitation minimaux sans lesquels l'entreprise resterait
    `PENDING_CONFIGURATION` et ne traiterait rien. L'ICE, lui, reste vide.
    """

    country: str = "MA"
    currency: str = "MAD"
    allowed_vat_rates: tuple[str, ...] = ("20",)
    telegram_chat_id: str = ""
    account_mapping: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProvisionResult:
    """Ce qui a REELLEMENT ete cree, pour le journal et les tests."""

    company_id: str = ""
    alias: str = ""
    created: bool = False
    sheet_id: str = ""
    drive_folder_id: str = ""
    activated: bool = False
    refused: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.company_id) and self.activated


def base_local_part(address: str) -> str:
    """Partie locale d'une adresse, sans le sous-adressage `+tag`."""
    propre = registry.normalize_alias(address)
    local = propre.split("@", 1)[0] if "@" in propre else propre
    return local.split("+", 1)[0]


def slug_from_address(address: str, base_address: str) -> str:
    """Identifiant demande par une adresse sous-adressee, ou chaine vide.

    Rend l'identifiant SEULEMENT si l'adresse est une sous-adresse de la
    boite de l'exploitant : meme partie locale de base, meme domaine.
    """
    recue = registry.normalize_alias(address)
    base = registry.normalize_alias(base_address)
    if not recue or not base or "@" not in recue or "@" not in base:
        return ""
    local, _, domaine = recue.partition("@")
    base_local, _, base_domaine = base.partition("@")
    if domaine != base_domaine:
        return ""
    if "+" not in local:
        return ""
    racine, _, tag = local.partition("+")
    if racine != base_local.split("+", 1)[0]:
        return ""
    tag = tag.strip().lower()
    return tag if SLUG_RE.match(tag) else ""


def candidate_slugs(message: dict[str, Any], base_address: str) -> tuple[str, ...]:
    """Identifiants candidats portes par les adresses de reception.

    L'ordre reprend celui du routage : les en-tetes de livraison, que
    l'expediteur ne controle pas, avant `To`/`Cc`.
    """
    entetes = routing._headers_map(message)  # noqa: SLF001 - meme couche de routage
    livraison = routing._addresses(  # noqa: SLF001
        v for nom in routing._DELIVERY_HEADERS for v in entetes.get(nom, ())
    )
    destinataires = routing._addresses(  # noqa: SLF001
        v for nom in routing._RECIPIENT_HEADERS for v in entetes.get(nom, ())
    )
    if not destinataires and message.get("to"):
        destinataires = routing._addresses([str(message.get("to"))])  # noqa: SLF001

    vus: list[str] = []
    for adresse in tuple(livraison) + tuple(destinataires):
        tag = slug_from_address(adresse, base_address)
        if tag and tag not in vus:
            vus.append(tag)
    return tuple(vus)


def alias_for(slug: str, base_address: str) -> str:
    """Adresse complete qui appartiendra a l'entreprise creee."""
    base = registry.normalize_alias(base_address)
    local, _, domaine = base.partition("@")
    return f"{local.split('+', 1)[0]}+{slug}@{domaine}"


class AutoProvisioner:
    """Cree l'entreprise designee par une adresse, puis s'efface.

    Le repartiteur l'appelle UNIQUEMENT quand le routage a repondu
    « entreprise inconnue » : le chemin nominal n'en depend pas.
    """

    def __init__(
        self,
        db_path: str,
        *,
        base_address: str,
        sheets: Any,
        drive: Any,
        template_sheet_id: str,
        defaults: ProvisionDefaults | None = None,
        max_companies: int = 50,
        reserved: Iterable[str] = (),
    ) -> None:
        self._db = db_path
        self._base = registry.normalize_alias(base_address)
        self._sheets = sheets
        self._drive = drive
        self._template = template_sheet_id
        self._defaults = defaults or ProvisionDefaults()
        self._max = int(max_companies)
        self._reserved = frozenset(RESERVED_SLUGS) | {
            str(r).strip().lower() for r in reserved if str(r).strip()
        }

    @property
    def configured(self) -> bool:
        """Vrai seulement si tout ce qu'exige une creation est present."""
        return bool(self._base and self._template and self._sheets and self._drive)

    def _refuse(self, slug: str, motif: str) -> ProvisionResult:
        logger.warning("Creation automatique refusee pour '%s' : %s", slug, motif)
        return ProvisionResult(company_id=slug, refused=motif)

    def provision(self, slug: str) -> ProvisionResult:
        """Cree UNE entreprise a partir d'un identifiant deja valide."""
        slug = str(slug or "").strip().lower()
        if not self.configured:
            return self._refuse(slug, "creation automatique non configuree")
        if not SLUG_RE.match(slug):
            return self._refuse(slug, "identifiant hors forme autorisee")
        if slug in self._reserved:
            return self._refuse(slug, "identifiant reserve")

        identifiant = registry.normalize_company_id(slug)
        existante = registry.get_company(self._db, identifiant)
        if existante is not None:
            # Le routage normal reprend la main : on ne recree jamais.
            return ProvisionResult(
                company_id=identifiant, alias=alias_for(slug, self._base),
                sheet_id=existante.sheet_id,
                drive_folder_id=existante.drive_folder_id,
                activated=existante.can_write,
            )

        registry.ensure_schema(self._db)
        connues = registry.list_companies(self._db)
        if len(connues) >= self._max:
            return self._refuse(
                slug,
                f"plafond de {self._max} entreprises atteint : aucune creation",
            )

        alias = alias_for(slug, self._base)
        # La raison sociale vaut l'identifiant : c'est la seule chose que
        # l'exploitant ait reellement ecrite. L'ICE reste VIDE.
        registry.register_company(
            self._db, identifiant,
            legal_name=slug.upper(),
            display_name=slug.upper(),
            inbound_aliases=[alias],
            country=self._defaults.country,
            currency=self._defaults.currency,
            allowed_vat_rates=list(self._defaults.allowed_vat_rates),
            telegram_chat_id=str(self._defaults.telegram_chat_id or ""),
            account_mapping=dict(self._defaults.account_mapping or {}),
        )
        logger.info(
            "Entreprise creee automatiquement depuis l'adresse %s : %s",
            alias, identifiant,
        )

        try:
            rapport = amorce.bootstrap_company(
                self._db, identifiant, sheets=self._sheets, drive=self._drive,
                template_sheet_id=self._template,
            )
        except Exception as exc:  # noqa: BLE001 - un echec ne casse pas le cycle
            return self._refuse(slug, f"bootstrap impossible : {type(exc).__name__}: {exc}")

        resultat = ProvisionResult(
            company_id=identifiant, alias=alias, created=True,
            sheet_id=rapport.sheet_id, drive_folder_id=rapport.drive_folder_id,
            activated=bool(rapport.activated),
        )
        if not resultat.activated:
            resultat.refused = ", ".join(
                tuple(rapport.blocked_by or ()) + tuple(rapport.missing_tabs or ())
            ) or "bootstrap incomplet"
            logger.warning(
                "[%s] entreprise creee mais non activee : %s",
                identifiant, resultat.refused,
            )
        else:
            logger.info(
                "[%s] comptabilite prete : classeur %s, dossier %s",
                identifiant, resultat.sheet_id, resultat.drive_folder_id,
            )
        return resultat

    def provision_for_message(self, message: dict[str, Any]) -> ProvisionResult:
        """Cree l'entreprise designee par l'adresse de reception d'un email.

        Une seule creation par email : deux adresses inconnues dans le
        meme message signalent une erreur d'envoi, pas deux comptabilites
        a ouvrir.
        """
        if not self.configured:
            return ProvisionResult()
        candidats = candidate_slugs(message, self._base)
        if not candidats:
            return ProvisionResult()
        if len(candidats) > 1:
            return self._refuse(
                ", ".join(candidats),
                "plusieurs identifiants inconnus dans le meme email : "
                "aucune creation automatique",
            )
        return self.provision(candidats[0])
