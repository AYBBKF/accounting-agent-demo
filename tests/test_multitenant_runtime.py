"""Le branchement multi-entreprises : ce que fait REELLEMENT le demarrage.

Le code multi-tenant peut etre parfait et rester inerte si le processus
ne l'appelle pas. Ces tests portent sur la bascule elle-meme : quand elle
s'active, ce qu'elle refuse, et ce qu'elle laisse intact quand elle est
absente.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app import companies as registry
from app import doc_store as store
from app import multitenant_runtime as runtime
from app.db import init_db

XBLASTE_ALIAS = "faridrani438+xblaste@gmail.com"
SMOKE_ALIAS = "faridrani438+v2smoke@gmail.com"
CHAT = "999653395"


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    init_db(chemin)
    store.ensure_schema(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def _declaration(company_id, alias, **extra):
    base = {
        "company_id": company_id,
        "inbound_aliases": [alias],
        "display_name": company_id.upper(),
        "country": "MA",
        "currency": "MAD",
        "allowed_vat_rates": ["0", "7", "10", "20"],
        "telegram_chat_id": CHAT,
    }
    base.update(extra)
    return base


# --- lecture de la declaration --------------------------------------------


def test_une_declaration_valide_est_lue(db_path):
    brut = json.dumps([_declaration("xblaste", XBLASTE_ALIAS)])
    lues = runtime.parse_companies(brut)
    assert len(lues) == 1
    assert lues[0]["company_id"] == "xblaste"


def test_une_declaration_illisible_fait_echouer_franchement():
    """L'ignorer ferait croire a l'exploitant que ses clients sont servis."""
    with pytest.raises(runtime.RuntimeConfigError, match="illisible"):
        runtime.parse_companies("{ceci n'est pas du json")


def test_un_champ_inconnu_est_refuse():
    """Une faute de frappe produirait une entreprise incomplete silencieuse."""
    brut = json.dumps([{**_declaration("xblaste", XBLASTE_ALIAS), "devise": "MAD"}])
    with pytest.raises(runtime.RuntimeConfigError, match="devise"):
        runtime.parse_companies(brut)


def test_un_alias_manquant_est_refuse():
    """Sans alias, l'entreprise ne peut recevoir aucun email : c'est un piege."""
    brut = json.dumps([{"company_id": "xblaste"}])
    with pytest.raises(runtime.RuntimeConfigError, match="inbound_aliases"):
        runtime.parse_companies(brut)


def test_une_declaration_vide_ne_declare_rien():
    assert runtime.parse_companies("") == []
    assert runtime.parse_companies("   ") == []


# --- declaration au registre ----------------------------------------------


def test_les_entreprises_declarees_sont_inscrites(db_path):
    runtime.declare_companies(db_path, [
        _declaration("xblaste", XBLASTE_ALIAS),
        _declaration("v2-smoke", SMOKE_ALIAS),
    ])
    connues = {c.company_id for c in registry.list_companies(db_path)}
    assert connues == {"xblaste", "v2-smoke"}


def test_une_redeclaration_ne_perd_pas_le_classeur_acquis(db_path):
    """Le bootstrap a coute une copie de classeur : un redemarrage ne
    doit pas la jeter et en recreer une autre."""
    runtime.declare_companies(db_path, [_declaration("xblaste", XBLASTE_ALIAS)])
    registry.update_company(
        db_path, "xblaste", sheet_id="sheet-acquis", drive_folder_id="drive-acquis"
    )
    runtime.declare_companies(db_path, [_declaration("xblaste", XBLASTE_ALIAS)])

    entreprise = registry.get_company(db_path, "xblaste")
    assert entreprise.sheet_id == "sheet-acquis"
    assert entreprise.drive_folder_id == "drive-acquis"


def test_une_redeclaration_ne_reactive_pas_une_entreprise_suspendue(db_path):
    """Le statut est une decision, pas de la configuration."""
    runtime.declare_companies(db_path, [_declaration("xblaste", XBLASTE_ALIAS)])
    registry.set_status(db_path, "xblaste", registry.SUSPENDED)
    runtime.declare_companies(
        db_path, [_declaration("xblaste", XBLASTE_ALIAS, status=registry.ACTIVE)]
    )
    assert registry.get_company(db_path, "xblaste").status == registry.SUSPENDED


# --- preparation complete --------------------------------------------------


def test_la_preparation_migre_puis_declare(db_path):
    rapport = runtime.prepare(
        db_path,
        companies_json=json.dumps([_declaration("xblaste", XBLASTE_ALIAS)]),
    )
    assert rapport.migrated is True
    assert rapport.declared == ("xblaste",)


def test_une_entreprise_sans_classeur_n_est_pas_ecrivable(db_path):
    rapport = runtime.prepare(
        db_path,
        companies_json=json.dumps([_declaration("xblaste", XBLASTE_ALIAS)]),
    )
    assert rapport.writable == (), (
        "sans classeur ni dossier Drive, une entreprise ne doit rien traiter"
    )


def test_une_entreprise_complete_est_ecrivable(db_path):
    declaration = _declaration(
        "xblaste", XBLASTE_ALIAS, sheet_id="sheet-x", drive_folder_id="drive-x",
        status=registry.ACTIVE,
    )
    rapport = runtime.prepare(db_path, companies_json=json.dumps([declaration]))
    assert rapport.writable == ("xblaste",)


def test_la_preparation_est_rejouable(db_path):
    """Le conteneur redemarre : le demarrage doit etre sans effet."""
    brut = json.dumps([_declaration(
        "xblaste", XBLASTE_ALIAS, sheet_id="sheet-x",
        drive_folder_id="drive-x", status=registry.ACTIVE,
    )])
    premier = runtime.prepare(db_path, companies_json=brut)
    for _ in range(3):
        rejeu = runtime.prepare(db_path, companies_json=brut)
        assert rejeu.writable == premier.writable
        assert rejeu.migrated is False
    assert len(registry.list_companies(db_path)) == 1


# --- bootstrap au demarrage ------------------------------------------------


class FauxSheets:
    def __init__(self, tabs=None) -> None:
        self.copies: list[tuple[str, str]] = []
        self.vides: list[tuple[str, str]] = []
        self._tabs = tabs

    def copy_spreadsheet(self, source_id, title):
        self.copies.append((source_id, title))
        return f"sheet-{len(self.copies)}"

    def list_tabs(self, spreadsheet_id):
        from app.bootstrap import REQUIRED_TABS

        return list(self._tabs if self._tabs is not None else REQUIRED_TABS)

    def clear_range(self, spreadsheet_id, plage):
        self.vides.append((spreadsheet_id, plage))


class FauxDrive:
    def __init__(self) -> None:
        self.crees: list[str] = []

    def find_folder(self, name):
        return ""

    def create_folder(self, name):
        self.crees.append(name)
        return f"drive-{len(self.crees)}"


def test_le_bootstrap_au_demarrage_active_une_entreprise(db_path):
    sheets, drive = FauxSheets(), FauxDrive()
    declaration = _declaration(
        "v2-smoke", SMOKE_ALIAS, legal_name="Tenant de test", ice="000000000000000"
    )
    rapport = runtime.prepare(
        db_path, companies_json=json.dumps([declaration]),
        sheets=sheets, drive=drive, template_sheet_id="modele-xblaste",
    )
    assert rapport.activated == ("v2-smoke",)
    assert rapport.writable == ("v2-smoke",)
    assert sheets.copies == [("modele-xblaste", sheets.copies[0][1])]
    assert drive.crees, "un dossier Drive doit avoir ete cree"


def test_le_modele_n_est_jamais_vide_par_le_demarrage(db_path):
    """On copie le classeur XBLASTE ; on ne touche pas a l'original."""
    sheets, drive = FauxSheets(), FauxDrive()
    runtime.prepare(
        db_path,
        companies_json=json.dumps([_declaration(
            "v2-smoke", SMOKE_ALIAS, legal_name="Tenant de test",
            ice="000000000000000")]),
        sheets=sheets, drive=drive, template_sheet_id="modele-xblaste",
    )
    vides = {cible for cible, _ in sheets.vides}
    assert "modele-xblaste" not in vides


def test_une_entreprise_sans_donnees_legales_reste_en_attente(db_path):
    """Cas Flux Intelligent : le code va au bout, le tenant reste inactif."""
    sheets, drive = FauxSheets(), FauxDrive()
    rapport = runtime.prepare(
        db_path,
        companies_json=json.dumps([
            _declaration("fluxintelligent", "faridrani438+fluxintelligent@gmail.com")
        ]),
        sheets=sheets, drive=drive, template_sheet_id="modele-xblaste",
    )
    assert "fluxintelligent" not in rapport.writable
    assert "fluxintelligent" in rapport.pending
    entreprise = registry.get_company(db_path, "fluxintelligent")
    assert entreprise.status == registry.PENDING_CONFIGURATION


def test_l_echec_d_une_entreprise_ne_bloque_pas_les_autres(db_path):
    sheets, drive = FauxSheets(), FauxDrive()
    rapport = runtime.prepare(
        db_path,
        companies_json=json.dumps([
            _declaration("fluxintelligent", "faridrani438+fluxintelligent@gmail.com"),
            _declaration("v2-smoke", SMOKE_ALIAS, legal_name="Tenant de test",
                         ice="000000000000000"),
        ]),
        sheets=sheets, drive=drive, template_sheet_id="modele-xblaste",
    )
    assert rapport.writable == ("v2-smoke",)
    assert "fluxintelligent" in rapport.pending


def test_un_classeur_ampute_laisse_l_entreprise_en_attente(db_path):
    sheets = FauxSheets(tabs=["02_CLIENTS"])
    rapport = runtime.prepare(
        db_path,
        companies_json=json.dumps([_declaration(
            "v2-smoke", SMOKE_ALIAS, legal_name="Tenant de test",
            ice="000000000000000")]),
        sheets=sheets, drive=FauxDrive(), template_sheet_id="modele-xblaste",
    )
    assert rapport.writable == ()
    assert "v2-smoke" in rapport.pending


def test_le_bootstrap_ne_se_rejoue_pas_sur_une_entreprise_deja_active(db_path):
    """Deux classeurs pour une entreprise serait une comptabilite dedoublee."""
    sheets, drive = FauxSheets(), FauxDrive()
    brut = json.dumps([_declaration(
        "v2-smoke", SMOKE_ALIAS, legal_name="Tenant de test",
        ice="000000000000000")])
    for _ in range(3):
        runtime.prepare(db_path, companies_json=brut, sheets=sheets, drive=drive,
                        template_sheet_id="modele-xblaste")
    assert len(sheets.copies) == 1
    assert len(drive.crees) == 1


# --- construction du repartiteur -------------------------------------------


class FauxSettings:
    composio_api_key = "cle"
    gmail_watch_chat_id = 999653395
    db_path = ""
    gmail_watch_query = (
        "in:inbox has:attachment "
        "{filename:pdf filename:zip filename:png filename:jpg filename:jpeg} "
        '-subject:"ACCOUNTING-VERIF"'
    )
    gmail_watch_interval_seconds = 60
    gmail_watch_max_per_cycle = 5

    def zip_limits(self):
        return None


def test_le_repartiteur_recoit_la_requete_telle_quelle(db_path):
    reglages = FauxSettings()
    reglages.db_path = db_path
    moteur = runtime.build_worker(reglages)
    assert moteur.query == FauxSettings.gmail_watch_query
    assert '-subject:"ACCOUNTING-VERIF"' in moteur.query


def test_une_requete_vide_empeche_le_demarrage(db_path):
    """Aucun repli silencieux vers une requete plus large."""
    reglages = FauxSettings()
    reglages.db_path = db_path
    reglages.gmail_watch_query = ""
    with pytest.raises(ValueError, match="requete"):
        runtime.build_worker(reglages)
