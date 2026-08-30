"""Bootstrap d'une entreprise : son classeur, son dossier, son activation.

Une entreprise autorisee mais sans classeur ne peut rien recevoir. Ce
module lui en fabrique un a partir du modele comptable, lui cree son
dossier Drive, verifie que la structure est complete, et ne la passe
ACTIVE qu'a ce moment-la.

Deux proprietes gouvernent l'ecriture :

  * IDEMPOTENT. Le bootstrap peut echouer au milieu - quota Sheets,
    reseau, permission - et sera relance. Chaque etape enregistre son
    resultat dans le registre AVANT de passer a la suivante : une reprise
    reutilise le classeur deja copie au lieu d'en creer un second. Deux
    classeurs pour une meme entreprise seraient pires qu'aucun : la
    comptabilite se scinderait en silence.

  * TOUT OU RIEN sur l'ACTIVATION. Une entreprise n'est jamais
    partiellement active. Si une seule verification echoue, elle reste
    PENDING_CONFIGURATION avec le motif, et aucun email n'est traite pour
    elle. Mieux vaut un email en attente qu'une ecriture dans un classeur
    incomplet.

Le classeur d'origine n'est JAMAIS modifie : on le copie, puis on vide la
copie. Le modele reste le classeur vivant d'une entreprise reelle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app import companies as registry

logger = logging.getLogger("demo_bot.bootstrap")

TEMPLATE_VERSION = "V2"

# Onglets sans lesquels la comptabilite ne peut pas fonctionner. La liste
# est explicite : deduire les onglets obligatoires du modele reviendrait a
# accepter une copie amputee le jour ou le modele lui-meme perdrait un
# onglet.
REQUIRED_TABS = (
    "02_CLIENTS",
    "03_FOURNISSEURS",
    "04_FACTURES_VENTES",
    "05_FACTURES_ACHATS",
    "06_RELEVE_BANCAIRE",
    "08_RAPPROCHEMENT",
    "14_IMPORTS_LOG",
    "16_LIGNES_FACTURES",
    "17_AVOIRS",
    "21_A_VERIFIER",
)

# Onglets dont les LIGNES sont videes dans la copie. Les en-tetes, les
# formules, les validations et les formats restent : c'est tout l'interet
# de partir d'un modele vivant plutot que d'un classeur reconstruit.
#
# Les onglets de parametrage et de tableau de bord n'y figurent pas : ils
# ne portent pas de donnees transactionnelles, et les vider casserait les
# formules de la copie.
TRANSACTIONAL_TABS = REQUIRED_TABS + (
    "07_CAISSE_DEPENSES",
    "11_IMPAYES",
    "12_JOURNAL_COMPTABLE",
    "13_ANOMALIES",
    "18_DOCUMENTS_COMMERCIAUX",
    "19_ECHEANCES_A_PAYER",
)

# Premiere ligne de donnees. La ligne 1 porte les en-tetes et ne se vide
# jamais.
_FIRST_DATA_ROW = 2
_CLEAR_RANGE = "A{row}:Z2000"


class BootstrapError(RuntimeError):
    """Le bootstrap n'a pas pu aller au bout."""


class SheetsGateway(Protocol):
    """Le minimum dont le bootstrap a besoin cote Sheets."""

    def copy_spreadsheet(self, source_id: str, title: str) -> str: ...
    def list_tabs(self, spreadsheet_id: str) -> list[str]: ...
    def clear_range(self, spreadsheet_id: str, a1_range: str) -> None: ...


class DriveGateway(Protocol):
    """Le minimum dont le bootstrap a besoin cote Drive."""

    def find_folder(self, name: str) -> str: ...
    def create_folder(self, name: str) -> str: ...


@dataclass
class BootstrapResult:
    """Ce que le bootstrap a REELLEMENT fait, etape par etape."""

    company_id: str
    sheet_id: str = ""
    drive_folder_id: str = ""
    sheet_created: bool = False
    drive_created: bool = False
    tabs_cleared: tuple[str, ...] = ()
    activated: bool = False
    reused: tuple[str, ...] = ()
    missing_tabs: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.activated


def workbook_title(company: registry.Company) -> str:
    """Nom explicite du classeur. Un nom generique rendrait deux
    comptabilites indiscernables dans un Drive partage."""
    nom = company.legal_name.strip() or company.display_name.strip() or company.company_id
    return f"COMPTABILITE {nom} [{company.company_id}]"


def drive_folder_name(company: registry.Company) -> str:
    nom = company.display_name.strip() or company.company_id
    return f"{nom} - Factures [{company.company_id}]"


def bootstrap_company(
    db_path: str,
    company_id: str,
    *,
    sheets: SheetsGateway,
    drive: DriveGateway,
    template_sheet_id: str,
    activate: bool = True,
) -> BootstrapResult:
    """Prepare le classeur et le dossier d'une entreprise, puis l'active.

    Relancable : les ressources deja creees sont reutilisees, jamais
    dupliquees.
    """
    entreprise = registry.get_company(db_path, company_id)
    if entreprise is None:
        raise BootstrapError(f"entreprise inconnue : '{company_id}'")
    if entreprise.status == registry.DISABLED:
        raise BootstrapError(
            f"l'entreprise '{entreprise.company_id}' est DISABLED : "
            "son bootstrap doit etre demande explicitement par un administrateur"
        )

    resultat = BootstrapResult(company_id=entreprise.company_id)
    reutilise: list[str] = []

    # --- 1. le classeur ---------------------------------------------------
    if entreprise.sheet_id:
        # Une reprise apres echec ne recopie pas : deux classeurs
        # scinderaient la comptabilite en silence.
        resultat.sheet_id = entreprise.sheet_id
        reutilise.append("sheet")
    else:
        if not template_sheet_id:
            raise BootstrapError("aucun modele comptable fourni")
        nouveau = sheets.copy_spreadsheet(
            template_sheet_id, workbook_title(entreprise)
        )
        if not nouveau:
            raise BootstrapError("la copie du modele comptable n'a rien rendu")
        # Enregistre AVANT toute suite : si l'etape suivante echoue, la
        # reprise retrouvera ce classeur au lieu d'en creer un second.
        registry.update_company(db_path, entreprise.company_id, sheet_id=nouveau)
        resultat.sheet_id = nouveau
        resultat.sheet_created = True
        logger.info(
            "[%s] classeur cree depuis le modele : %s",
            entreprise.company_id, nouveau,
        )

    # --- 2. le dossier Drive ---------------------------------------------
    if entreprise.drive_folder_id:
        resultat.drive_folder_id = entreprise.drive_folder_id
        reutilise.append("drive")
    else:
        nom = drive_folder_name(entreprise)
        # On cherche AVANT de creer : un dossier homonyme laisse par une
        # tentative interrompue doit etre repris, pas double.
        existant = drive.find_folder(nom)
        if existant:
            resultat.drive_folder_id = existant
            reutilise.append("drive")
        else:
            cree = drive.create_folder(nom)
            if not cree:
                raise BootstrapError("la creation du dossier Drive n'a rien rendu")
            resultat.drive_folder_id = cree
            resultat.drive_created = True
        registry.update_company(
            db_path, entreprise.company_id,
            drive_folder_id=resultat.drive_folder_id,
        )

    # --- 3. structure complete ? -----------------------------------------
    onglets = set(sheets.list_tabs(resultat.sheet_id))
    manquants = tuple(t for t in REQUIRED_TABS if t not in onglets)
    if manquants:
        resultat.missing_tabs = manquants
        resultat.blocked_by = (
            f"onglets obligatoires absents du classeur : {', '.join(manquants)}",
        )
        registry.update_company(
            db_path, entreprise.company_id,
            config_validation_status=registry.CONFIG_INCOMPLETE,
        )
        logger.warning(
            "[%s] bootstrap incomplet : %s", entreprise.company_id, manquants
        )
        return resultat

    # --- 4. vider les donnees transactionnelles --------------------------
    # Seulement les lignes. En-tetes, formules, validations et formats
    # restent ceux du modele.
    vides: list[str] = []
    for onglet in TRANSACTIONAL_TABS:
        if onglet not in onglets:
            continue
        sheets.clear_range(
            resultat.sheet_id,
            f"{onglet}!{_CLEAR_RANGE.format(row=_FIRST_DATA_ROW)}",
        )
        vides.append(onglet)
    resultat.tabs_cleared = tuple(vides)

    registry.update_company(
        db_path, entreprise.company_id,
        template_version=TEMPLATE_VERSION,
        config_validation_status=registry.CONFIG_OK,
    )

    # --- 5. activation, seulement si tout le reste est la ----------------
    a_jour = registry.get_company(db_path, entreprise.company_id)
    assert a_jour is not None
    manque = a_jour.missing_for_activation
    if manque:
        # Typiquement les donnees legales d'une entreprise dont on ne
        # veut RIEN inventer : le classeur est pret, l'entreprise reste
        # en attente et ne traite aucun email.
        resultat.blocked_by = (
            f"configuration incomplete : {', '.join(manque)}",
        )
        resultat.reused = tuple(reutilise)
        logger.info(
            "[%s] classeur pret, activation differee : %s",
            entreprise.company_id, manque,
        )
        return resultat

    if activate:
        registry.set_status(db_path, entreprise.company_id, registry.ACTIVE)
        resultat.activated = True
        logger.info("[%s] entreprise activee", entreprise.company_id)

    resultat.reused = tuple(reutilise)
    return resultat


def verify_workbook(sheets: SheetsGateway, spreadsheet_id: str) -> tuple[str, ...]:
    """Onglets obligatoires absents d'un classeur. Vide = conforme.

    Sert aussi en dehors du bootstrap : un classeur peut perdre un onglet
    apres coup, et l'agent doit s'en apercevoir avant d'ecrire.
    """
    presents = set(sheets.list_tabs(spreadsheet_id))
    return tuple(t for t in REQUIRED_TABS if t not in presents)


# --- adaptateurs reels ----------------------------------------------------
#
# Le bootstrap parle a des Protocol, pas a Composio : c'est ce qui le rend
# testable sans reseau. Ces deux adaptateurs branchent ces Protocol sur la
# passerelle deja utilisee par le reste de l'agent.


class ComposioSheets:
    """Implemente `SheetsGateway` au-dessus de la passerelle Composio."""

    def __init__(self, gateway: Any) -> None:
        self._gw = gateway

    def copy_spreadsheet(self, source_id: str, title: str) -> str:
        data = self._gw.execute(
            "GOOGLEDRIVE_COPY_FILE", {"file_id": source_id, "new_title": title}
        )
        return str((data or {}).get("id") or "")

    def list_tabs(self, spreadsheet_id: str) -> list[str]:
        data = self._gw.execute(
            "GOOGLESHEETS_GET_SPREADSHEET_INFO", {"spreadsheet_id": spreadsheet_id}
        )
        onglets: list[str] = []
        for feuille in (data or {}).get("sheets", []):
            titre = ((feuille or {}).get("properties") or {}).get("title")
            if titre:
                onglets.append(str(titre))
        return onglets

    def clear_range(self, spreadsheet_id: str, a1_range: str) -> None:
        self._gw.execute(
            "GOOGLESHEETS_CLEAR_VALUES",
            {"spreadsheet_id": spreadsheet_id, "range": a1_range},
        )


class ComposioDrive:
    """Implemente `DriveGateway` au-dessus de la passerelle Composio."""

    def __init__(self, gateway: Any, parent_folder_id: str = "") -> None:
        self._gw = gateway
        self._parent = parent_folder_id

    def find_folder(self, name: str) -> str:
        requete: dict[str, Any] = {"name_exact": name, "page_size": 10}
        if self._parent:
            requete["parent_folder_id"] = self._parent
        try:
            data = self._gw.execute("GOOGLEDRIVE_FIND_FOLDER", requete)
        except Exception:  # noqa: BLE001 - absence de dossier, pas une panne
            return ""
        return _premier_dossier(data)

    def create_folder(self, name: str) -> str:
        args: dict[str, Any] = {"name": name}
        if self._parent:
            args["parent_id"] = self._parent
        data = self._gw.execute("GOOGLEDRIVE_CREATE_FOLDER", args)
        return _premier_dossier(data)


def _premier_dossier(data: Any) -> str:
    """Identifiant du premier dossier d'une reponse Drive, ou chaine vide.

    Drive rend tantot un objet, tantot une liste : les deux formes ont ete
    observees selon l'outil appele.
    """
    if not isinstance(data, dict):
        return ""
    if data.get("id"):
        return str(data["id"])
    for cle in ("files", "folders", "data"):
        valeurs = data.get(cle)
        if isinstance(valeurs, list) and valeurs:
            premier = valeurs[0]
            if isinstance(premier, dict) and premier.get("id"):
                return str(premier["id"])
    return ""
