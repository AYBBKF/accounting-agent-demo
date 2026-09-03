"""Le repartiteur fait naitre l'entreprise, puis lui confie l'email.

Le test precedent prouve que la creation fonctionne ; celui-ci prouve
qu'elle est REELLEMENT branchee sur le cycle Gmail : un email adresse a
une entreprise qui n'existe pas encore doit finir traite par elle, dans
le meme cycle, sans intervention humaine. Sans le branchement, il repart
en quarantaine `unknown_company`.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import auto_provision as ap
from app import bootstrap as amorce
from app import companies as registry
from app import routing
from app.tenant_worker import TenantWorker

BASE = "faridrani438@gmail.com"
QUERY = "in:inbox has:attachment"


class _Sheets:
    def __init__(self):
        self.copies = []

    def copy_spreadsheet(self, source_id, title):
        self.copies.append(title)
        return f"sheet-{len(self.copies)}"

    def list_tabs(self, spreadsheet_id):
        return list(amorce.REQUIRED_TABS)

    def clear_range(self, spreadsheet_id, a1_range):
        return None


class _Drive:
    def find_folder(self, name):
        return ""

    def create_folder(self, name):
        return "folder-1"


class _FauxWorker:
    """Worker d'entreprise minimal : retient ce qu'on lui a confie."""

    def __init__(self, tenant):
        self.company_id = tenant.company_id
        self.sheet_id = tenant.sheet_id
        self.traites: list[str] = []

    def process_message(self, message_id):
        self.traites.append(message_id)
        return None, 0


@pytest.fixture
def db_path():
    from app import doc_store as store
    from app import tenancy
    from app.db import init_db

    chemin = tempfile.mktemp(suffix=".db")
    init_db(chemin)
    store.ensure_schema(chemin)
    tenancy.migrate_to_multi_tenant(chemin)
    registry.ensure_schema(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def _message():
    return {"payload": {"headers": [
        {"name": "Delivered-To", "value": "faridrani438+argana78@gmail.com"},
        {"name": "Subject", "value": "DOSSIER-COMPTABLE-AOUT-2026"},
        {"name": "From", "value": "expediteur@example.invalid"},
    ]}}


def _repartiteur(db_path, monkeypatch, provisioner):
    workers: dict[str, _FauxWorker] = {}

    def fabrique(tenant):
        cree = _FauxWorker(tenant)
        workers[tenant.company_id] = cree
        return cree

    moteur = TenantWorker(
        api_key="k", chat_id=999653395, db_path=db_path, query=QUERY,
        worker_factory=fabrique, provisioner=provisioner,
    )
    monkeypatch.setattr(
        moteur.probe, "fetch_message", lambda mid: _message(), raising=False
    )
    monkeypatch.setattr(
        moteur, "search_messages", lambda: [{"messageId": "m-argana"}]
    )
    return moteur, workers


def test_sans_provisionneur_l_email_reste_en_quarantaine(db_path, monkeypatch):
    """Comportement d'origine, inchange : aucune entreprise ne nait."""
    moteur, workers = _repartiteur(db_path, monkeypatch, None)
    rapport = moteur.process_once()
    assert rapport.emails[0].outcome == routing.UNKNOWN_COMPANY
    assert not workers
    assert registry.get_company(db_path, "argana78") is None


def test_avec_provisionneur_l_entreprise_nait_et_traite_le_meme_email(
    db_path, monkeypatch
):
    """Le contrat demande : le classeur est cree, PUIS les ecritures
    commencent - dans le meme cycle, sur le meme email."""
    sheets = _Sheets()
    provisionneur = ap.AutoProvisioner(
        db_path, base_address=BASE, sheets=sheets, drive=_Drive(),
        template_sheet_id="modele-123",
        defaults=ap.ProvisionDefaults(telegram_chat_id="999653395"),
    )
    moteur, workers = _repartiteur(db_path, monkeypatch, provisionneur)
    rapport = moteur.process_once()

    entree = rapport.emails[0]
    assert entree.company_id == "argana78", entree.reason
    assert entree.outcome == routing.ROUTED
    assert "argana78" in workers
    assert workers["argana78"].traites == ["m-argana"]
    # Le classeur porte bien le nom demande.
    assert sheets.copies and "ARGANA78" in sheets.copies[0]
    creee = registry.get_company(db_path, "argana78")
    assert creee is not None and creee.can_write


def test_l_entreprise_creee_n_est_pas_recreee_au_cycle_suivant(db_path, monkeypatch):
    sheets = _Sheets()
    provisionneur = ap.AutoProvisioner(
        db_path, base_address=BASE, sheets=sheets, drive=_Drive(),
        template_sheet_id="modele-123",
        defaults=ap.ProvisionDefaults(telegram_chat_id="999653395"),
    )
    moteur, _ = _repartiteur(db_path, monkeypatch, provisionneur)
    moteur.process_once()
    moteur.process_once()
    assert len(sheets.copies) == 1
