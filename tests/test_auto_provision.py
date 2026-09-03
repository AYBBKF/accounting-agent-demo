"""Une adresse sous-adressee fait naitre sa comptabilite.

Demande de l'exploitant : un email livre a `base+argana78@gmail.com`
doit creer la comptabilite `argana78` - classeur au meme nom, dossier
Drive, registre - PUIS ecrire. Avant ce module, le routage repondait
`unknown_company` et l'email restait en quarantaine pour toujours.

Ces tests verrouillent les deux moities du contrat : ce qui doit naitre,
et surtout ce qui ne doit JAMAIS naitre.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import auto_provision as ap
from app import companies as registry
from app import routing

BASE = "faridrani438@gmail.com"


# --- ce que porte une adresse --------------------------------------------

def test_une_sous_adresse_designe_un_identifiant():
    assert ap.slug_from_address("faridrani438+argana78@gmail.com", BASE) == "argana78"
    assert ap.slug_from_address("FaridRani438+ARGANA78@Gmail.com", BASE) == "argana78"


def test_une_adresse_d_une_autre_boite_ne_designe_rien():
    """Le sous-adressage d'un tiers ne cree rien chez nous."""
    assert ap.slug_from_address("quelqundautre+argana78@gmail.com", BASE) == ""
    assert ap.slug_from_address("faridrani438+argana78@autre.com", BASE) == ""


def test_une_adresse_sans_tag_ne_designe_rien():
    assert ap.slug_from_address(BASE, BASE) == ""


def test_un_tag_hors_forme_est_refuse():
    """On ne « nettoie » pas un identifiant : on le refuse."""
    for mauvais in ("a", "-argana", "argana 78", "argana/78", "ARGANA_78",
                    "x" * 41, "argana78@", ""):
        assert ap.slug_from_address(
            f"faridrani438+{mauvais}@gmail.com", BASE
        ) == "", mauvais


def test_l_alias_reconstruit_est_celui_de_la_boite():
    assert ap.alias_for("argana78", BASE) == "faridrani438+argana78@gmail.com"


# --- passerelles de test --------------------------------------------------

class _Sheets:
    """Copie un modele et rend un classeur complet."""

    def __init__(self, onglets=None):
        self.copies: list[tuple[str, str]] = []
        self.vides: list[tuple[str, str]] = []
        self._onglets = list(
            onglets if onglets is not None else __import__(
                "app.bootstrap", fromlist=["x"]
            ).REQUIRED_TABS
        )

    def copy_spreadsheet(self, source_id, title):
        self.copies.append((source_id, title))
        return f"sheet-{len(self.copies)}"

    def list_tabs(self, spreadsheet_id):
        return list(self._onglets)

    def clear_range(self, spreadsheet_id, a1_range):
        self.vides.append((spreadsheet_id, a1_range))


class _Drive:
    def __init__(self):
        self.crees: list[str] = []

    def find_folder(self, name):
        return ""

    def create_folder(self, name):
        self.crees.append(name)
        return f"folder-{len(self.crees)}"


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    registry.ensure_schema(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def _provisionneur(db_path, sheets=None, drive=None, **kw):
    return ap.AutoProvisioner(
        db_path, base_address=BASE,
        sheets=sheets or _Sheets(), drive=drive or _Drive(),
        template_sheet_id="modele-123",
        defaults=ap.ProvisionDefaults(telegram_chat_id="999653395"),
        **kw,
    )


def _email(*destinataires, livraison=()):
    entetes = [{"name": "Subject", "value": "DOSSIER COMPTABLE"},
               {"name": "From", "value": "quiconque@example.invalid"}]
    for adresse in livraison:
        entetes.append({"name": "Delivered-To", "value": adresse})
    for adresse in destinataires:
        entetes.append({"name": "To", "value": adresse})
    return {"payload": {"headers": entetes}}


# --- creation -------------------------------------------------------------

def test_une_adresse_inconnue_cree_le_classeur_a_son_nom(db_path):
    """Le coeur de la demande : classeur et dossier portent l'identifiant."""
    sheets, drive = _Sheets(), _Drive()
    resultat = _provisionneur(db_path, sheets, drive).provision_for_message(
        _email(livraison=["faridrani438+argana78@gmail.com"])
    )
    assert resultat.created and resultat.usable, resultat.refused
    assert resultat.company_id == "argana78"
    assert len(sheets.copies) == 1
    source, titre = sheets.copies[0]
    assert source == "modele-123"
    assert "ARGANA78" in titre and "argana78" in titre
    assert drive.crees and "ARGANA78" in drive.crees[0]


def test_l_entreprise_creee_est_immediatement_routable(db_path):
    """Creer sans rendre routable ne servirait a rien : l'email suivant
    doit etre attribue a cette comptabilite, pas mis en quarantaine."""
    message = _email(livraison=["faridrani438+argana78@gmail.com"])
    avant = routing.route_message(db_path, message)
    assert avant.outcome == routing.UNKNOWN_COMPANY

    _provisionneur(db_path).provision_for_message(message)

    apres = routing.route_message(db_path, message)
    assert apres.accepted and apres.company_id == "argana78"


def test_l_identite_fiscale_n_est_jamais_inventee(db_path):
    """Raison sociale = identifiant, ICE VIDE. Un ICE fabrique ferait
    entrer une identite legale que personne n'a declaree."""
    _provisionneur(db_path).provision("argana78")
    creee = registry.get_company(db_path, "argana78")
    assert creee is not None
    assert creee.legal_name == "ARGANA78"
    assert creee.ice == ""
    assert creee.currency == "MAD"
    assert tuple(str(t) for t in creee.allowed_vat_rates) == ("20",)


def test_une_seconde_reception_ne_recree_rien(db_path):
    """Deux emails sur la meme adresse ne doivent pas scinder la
    comptabilite en deux classeurs."""
    sheets = _Sheets()
    prov = _provisionneur(db_path, sheets)
    message = _email(livraison=["faridrani438+argana78@gmail.com"])
    prov.provision_for_message(message)
    second = prov.provision_for_message(message)
    assert len(sheets.copies) == 1
    assert second.created is False and second.usable


# --- ce qui ne doit jamais naitre ----------------------------------------

def test_le_sujet_ne_cree_jamais_une_entreprise(db_path):
    """Seule l'adresse de reception fait naitre. Un tag dans le sujet,
    lui, est controle par l'expediteur."""
    message = {"payload": {"headers": [
        {"name": "Subject", "value": "[ACCOUNTING:pirate] facture"},
        {"name": "To", "value": BASE},
        {"name": "From", "value": "attaquant@example.invalid"},
    ]}}
    resultat = _provisionneur(db_path).provision_for_message(message)
    assert resultat.company_id == ""
    assert registry.get_company(db_path, "pirate") is None


def test_un_identifiant_reserve_ne_nait_pas(db_path):
    resultat = _provisionneur(db_path).provision_for_message(
        _email(livraison=["faridrani438+xblaste@gmail.com"])
    )
    assert not resultat.usable and "reserve" in resultat.refused
    assert registry.get_company(db_path, "xblaste") is None


def test_deux_adresses_inconnues_dans_un_email_ne_creent_rien(db_path):
    """Deux alias inconnus revelent une erreur d'envoi, pas deux
    comptabilites a ouvrir."""
    resultat = _provisionneur(db_path).provision_for_message(
        _email("faridrani438+alpha@gmail.com", "faridrani438+beta@gmail.com")
    )
    assert not resultat.usable
    assert registry.get_company(db_path, "alpha") is None
    assert registry.get_company(db_path, "beta") is None


def test_le_plafond_bloque_l_avalanche(db_path):
    prov = _provisionneur(db_path, max_companies=2)
    assert prov.provision("alpha").usable
    assert prov.provision("beta").usable
    trop = prov.provision("gamma")
    assert not trop.usable and "plafond" in trop.refused
    assert registry.get_company(db_path, "gamma") is None


def test_un_classeur_modele_incomplet_n_active_pas(db_path):
    """Le classeur cree n'a pas la structure attendue : l'entreprise
    existe mais n'ecrit rien, plutot que d'ecrire n'importe ou."""
    sheets = _Sheets(onglets=["01_PARAMETRES"])
    resultat = _provisionneur(db_path, sheets).provision("argana78")
    assert not resultat.usable
    creee = registry.get_company(db_path, "argana78")
    assert creee is not None and not creee.can_write


def test_sans_configuration_aucune_creation(db_path):
    """Sans classeur modele, la creation automatique se tait."""
    muet = ap.AutoProvisioner(
        db_path, base_address=BASE, sheets=_Sheets(), drive=_Drive(),
        template_sheet_id="",
    )
    assert not muet.configured
    assert muet.provision_for_message(
        _email(livraison=["faridrani438+argana78@gmail.com"])
    ).company_id == ""
