"""Bootstrap d'une entreprise : reussite, echec, et reprise apres echec.

Le risque central de ce module n'est pas l'echec - c'est la REPRISE. Un
bootstrap interrompu puis relance ne doit jamais produire un second
classeur : la comptabilite de l'entreprise se scinderait alors en deux,
sans que personne ne s'en apercoive.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import bootstrap
from app import companies as registry

TEMPLATE = "modele-xblaste"


class FauxSheets:
    """Sheets simule, qui journalise ce qu'on lui demande."""

    def __init__(self, onglets: list[str] | None = None) -> None:
        self.copies: list[tuple[str, str]] = []
        self.effacements: list[tuple[str, str]] = []
        self.onglets = list(
            onglets if onglets is not None
            else bootstrap.TRANSACTIONAL_TABS + ("00_DASHBOARD", "01_PARAMETRES")
        )
        self.echec_copie = False
        self.echec_effacement_sur = ""

    def copy_spreadsheet(self, source_id: str, title: str) -> str:
        if self.echec_copie:
            raise RuntimeError("quota Sheets atteint")
        self.copies.append((source_id, title))
        return f"classeur-{len(self.copies)}"

    def list_tabs(self, spreadsheet_id: str) -> list[str]:
        return list(self.onglets)

    def clear_range(self, spreadsheet_id: str, a1_range: str) -> None:
        if self.echec_effacement_sur and a1_range.startswith(
            self.echec_effacement_sur
        ):
            raise RuntimeError("quota Sheets atteint")
        self.effacements.append((spreadsheet_id, a1_range))


class FauxDrive:
    def __init__(self) -> None:
        self.dossiers: dict[str, str] = {}
        self.creations: list[str] = []
        self.echec_creation = False

    def find_folder(self, name: str) -> str:
        return self.dossiers.get(name, "")

    def create_folder(self, name: str) -> str:
        if self.echec_creation:
            raise RuntimeError("Drive indisponible")
        self.creations.append(name)
        identifiant = f"dossier-{len(self.creations)}"
        self.dossiers[name] = identifiant
        return identifiant


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    registry.ensure_schema(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def _inscrire(db_path, identifiant="v2-smoke", **extra):
    champs = dict(
        display_name=identifiant, legal_name=f"{identifiant} SARL",
        status=registry.PENDING_CONFIGURATION,
        inbound_aliases=[f"faridrani438+{identifiant}@gmail.com"],
        country="MA", currency="MAD", allowed_vat_rates=["20"],
        telegram_chat_id="999653395",
    )
    champs.update(extra)
    return registry.register_company(db_path, identifiant, **champs)


def _lancer(db_path, sheets, drive, identifiant="v2-smoke"):
    return bootstrap.bootstrap_company(
        db_path, identifiant, sheets=sheets, drive=drive,
        template_sheet_id=TEMPLATE,
    )


# --- bootstrap reussi -----------------------------------------------------


def test_un_bootstrap_complet_active_l_entreprise(db_path):
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    resultat = _lancer(db_path, sheets, drive)

    assert resultat.succeeded
    assert resultat.activated
    assert resultat.sheet_created and resultat.drive_created
    assert registry.get_company(db_path, "v2-smoke").status == registry.ACTIVE
    assert registry.get_company(db_path, "v2-smoke").can_write


def test_le_classeur_est_une_copie_du_modele(db_path):
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    _lancer(db_path, sheets, drive)
    assert len(sheets.copies) == 1
    source, titre = sheets.copies[0]
    assert source == TEMPLATE
    assert "v2-smoke" in titre


def test_le_modele_d_origine_n_est_jamais_vide(db_path):
    """On copie, puis on vide la COPIE. Jamais le modele."""
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    resultat = _lancer(db_path, sheets, drive)
    cibles = {identifiant for identifiant, _ in sheets.effacements}
    assert cibles == {resultat.sheet_id}
    assert TEMPLATE not in cibles


def test_seules_les_lignes_de_donnees_sont_videes(db_path):
    """La ligne 1 porte les en-tetes, les formules et les validations."""
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    _lancer(db_path, sheets, drive)
    assert sheets.effacements
    for _, plage in sheets.effacements:
        assert "!A2:" in plage, f"plage suspecte : {plage}"


def test_les_onglets_de_parametrage_ne_sont_pas_touches(db_path):
    """Les vider casserait les formules de la copie."""
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    _lancer(db_path, sheets, drive)
    touches = {plage.split("!")[0] for _, plage in sheets.effacements}
    assert "00_DASHBOARD" not in touches
    assert "01_PARAMETRES" not in touches


def test_les_identifiants_sont_inscrits_au_registre(db_path):
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    resultat = _lancer(db_path, sheets, drive)
    entreprise = registry.get_company(db_path, "v2-smoke")
    assert entreprise.sheet_id == resultat.sheet_id
    assert entreprise.drive_folder_id == resultat.drive_folder_id
    assert entreprise.template_version == bootstrap.TEMPLATE_VERSION
    assert entreprise.config_validation_status == registry.CONFIG_OK


# --- idempotence et reprise ----------------------------------------------


def test_un_bootstrap_rejoue_ne_cree_pas_un_second_classeur(db_path):
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    premier = _lancer(db_path, sheets, drive)

    for _ in range(3):
        rejeu = _lancer(db_path, sheets, drive)
        assert rejeu.sheet_id == premier.sheet_id
        assert rejeu.drive_folder_id == premier.drive_folder_id
        assert not rejeu.sheet_created
        assert "sheet" in rejeu.reused and "drive" in rejeu.reused

    assert len(sheets.copies) == 1, "un seul classeur, quoi qu'il arrive"
    assert len(drive.creations) == 1


def test_une_reprise_apres_echec_drive_reutilise_le_classeur(db_path):
    """Le classeur a ete copie, puis Drive a lache. On relance."""
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    drive.echec_creation = True
    with pytest.raises(RuntimeError, match="Drive indisponible"):
        _lancer(db_path, sheets, drive)

    # Le classeur est deja enregistre : l'entreprise n'est pas active.
    entre_deux = registry.get_company(db_path, "v2-smoke")
    assert entre_deux.sheet_id
    assert entre_deux.status == registry.PENDING_CONFIGURATION

    drive.echec_creation = False
    reprise = _lancer(db_path, sheets, drive)
    assert reprise.succeeded
    assert len(sheets.copies) == 1, "aucun second classeur apres reprise"
    assert reprise.sheet_id == entre_deux.sheet_id


def test_une_reprise_reutilise_un_dossier_drive_homonyme(db_path):
    """Un dossier laisse par une tentative interrompue est repris."""
    entreprise = _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    drive.dossiers[bootstrap.drive_folder_name(entreprise)] = "dossier-existant"

    resultat = _lancer(db_path, sheets, drive)
    assert resultat.drive_folder_id == "dossier-existant"
    assert drive.creations == [], "aucun dossier en double"


def test_un_echec_de_copie_ne_laisse_aucune_entreprise_active(db_path):
    _inscrire(db_path)
    sheets, drive = FauxSheets(), FauxDrive()
    sheets.echec_copie = True
    with pytest.raises(RuntimeError):
        _lancer(db_path, sheets, drive)
    entreprise = registry.get_company(db_path, "v2-smoke")
    assert entreprise.status == registry.PENDING_CONFIGURATION
    assert not entreprise.can_write


# --- bootstrap incomplet --------------------------------------------------


def test_un_classeur_ampute_bloque_l_activation(db_path):
    """Mieux vaut un email en attente qu'une ecriture dans un classeur
    incomplet."""
    _inscrire(db_path)
    onglets = [t for t in bootstrap.TRANSACTIONAL_TABS if t != "05_FACTURES_ACHATS"]
    sheets, drive = FauxSheets(onglets), FauxDrive()
    resultat = _lancer(db_path, sheets, drive)

    assert not resultat.succeeded
    assert resultat.missing_tabs == ("05_FACTURES_ACHATS",)
    assert resultat.blocked_by
    entreprise = registry.get_company(db_path, "v2-smoke")
    assert entreprise.status == registry.PENDING_CONFIGURATION
    assert entreprise.config_validation_status == registry.CONFIG_INCOMPLETE
    assert sheets.effacements == [], "on ne vide rien d'un classeur ampute"


def test_une_entreprise_sans_donnees_legales_reste_en_attente(db_path):
    """Cas Flux Intelligent : le classeur est pret, l'entreprise non.

    On ne devine ni raison sociale, ni devise, ni TVA : le bootstrap
    prepare tout ce qu'il peut et s'arrete avant l'activation.
    """
    registry.register_company(
        db_path, "fluxintelligent", display_name="Flux Intelligent",
        status=registry.PENDING_CONFIGURATION,
        inbound_aliases=["faridrani438+fluxintelligent@gmail.com"],
    )
    sheets, drive = FauxSheets(), FauxDrive()
    resultat = _lancer(db_path, sheets, drive, "fluxintelligent")

    assert resultat.sheet_id and resultat.drive_folder_id
    assert not resultat.activated
    assert resultat.blocked_by
    entreprise = registry.get_company(db_path, "fluxintelligent")
    assert entreprise.status == registry.PENDING_CONFIGURATION
    assert not entreprise.can_write
    assert "currency" in entreprise.missing_for_activation


def test_une_entreprise_desactivee_ne_se_bootstrape_pas_toute_seule(db_path):
    _inscrire(db_path, status=registry.DISABLED)
    sheets, drive = FauxSheets(), FauxDrive()
    with pytest.raises(bootstrap.BootstrapError, match="DISABLED"):
        _lancer(db_path, sheets, drive)
    assert sheets.copies == []


def test_une_entreprise_inconnue_ne_se_bootstrape_jamais(db_path):
    """Aucun chemin ne cree une entreprise a partir d'un bootstrap."""
    sheets, drive = FauxSheets(), FauxDrive()
    with pytest.raises(bootstrap.BootstrapError, match="inconnue"):
        _lancer(db_path, sheets, drive, "societeinventee")
    assert registry.list_companies(db_path) == ()


# --- verification hors bootstrap -----------------------------------------


def test_un_classeur_qui_perd_un_onglet_est_detecte(db_path):
    """Un classeur peut perdre un onglet apres coup."""
    complet = FauxSheets()
    assert bootstrap.verify_workbook(complet, "classeur-1") == ()

    ampute = FauxSheets([t for t in bootstrap.REQUIRED_TABS if t != "21_A_VERIFIER"])
    assert bootstrap.verify_workbook(ampute, "classeur-1") == ("21_A_VERIFIER",)


# --- adaptateurs reels ----------------------------------------------------


class FausseePasserelle:
    def __init__(self, reponses: dict[str, object]) -> None:
        self.reponses = reponses
        self.appels: list[tuple[str, dict]] = []

    def execute(self, slug: str, arguments: dict) -> object:
        self.appels.append((slug, arguments))
        valeur = self.reponses.get(slug, {})
        if isinstance(valeur, Exception):
            raise valeur
        return valeur


def test_l_adaptateur_sheets_copie_et_liste():
    gw = FausseePasserelle({
        "GOOGLEDRIVE_COPY_FILE": {"id": "copie-1"},
        "GOOGLESHEETS_GET_SPREADSHEET_INFO": {
            "sheets": [
                {"properties": {"title": "05_FACTURES_ACHATS"}},
                {"properties": {"title": "21_A_VERIFIER"}},
            ]
        },
    })
    sheets = bootstrap.ComposioSheets(gw)
    assert sheets.copy_spreadsheet("modele", "Titre") == "copie-1"
    assert sheets.list_tabs("copie-1") == ["05_FACTURES_ACHATS", "21_A_VERIFIER"]
    sheets.clear_range("copie-1", "05_FACTURES_ACHATS!A2:Z2000")
    assert ("GOOGLESHEETS_CLEAR_VALUES", {
        "spreadsheet_id": "copie-1", "range": "05_FACTURES_ACHATS!A2:Z2000",
    }) in gw.appels


def test_l_adaptateur_drive_tolere_un_dossier_absent():
    """Une recherche infructueuse n'est pas une panne : on cree ensuite."""
    gw = FausseePasserelle({
        "GOOGLEDRIVE_FIND_FOLDER": RuntimeError("aucun resultat"),
        "GOOGLEDRIVE_CREATE_FOLDER": {"id": "dossier-9"},
    })
    drive = bootstrap.ComposioDrive(gw)
    assert drive.find_folder("Absent") == ""
    assert drive.create_folder("Absent") == "dossier-9"


def test_l_adaptateur_drive_lit_les_deux_formes_de_reponse():
    """Drive rend tantot un objet, tantot une liste."""
    liste = FausseePasserelle({"GOOGLEDRIVE_FIND_FOLDER": {
        "files": [{"id": "dossier-liste"}]
    }})
    assert bootstrap.ComposioDrive(liste).find_folder("X") == "dossier-liste"

    objet = FausseePasserelle({"GOOGLEDRIVE_FIND_FOLDER": {"id": "dossier-objet"}})
    assert bootstrap.ComposioDrive(objet).find_folder("X") == "dossier-objet"
