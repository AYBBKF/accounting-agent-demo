"""Isolation entre entreprises : la preuve qu'aucune donnee ne traverse.

Ces tests montent DEUX entreprises reelles dans une meme base, avec le
meme processus, puis verifient qu'aucune recherche, aucun doublon, aucun
curseur et aucune notification ne franchit la frontiere.

Le cas central du multi-tenant comptable : la MEME facture, aux memes
octets et au meme numero, envoyee a deux societes. Elle doit produire une
ecriture dans chacune. Une deduplication qui traverserait le tenant en
perdrait une - silencieusement.
"""

from __future__ import annotations

import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from app import companies as registry
from app import doc_store as store
from app import llm_usage
from app import tenancy
from app.db import init_db
from app.tenant_context import (
    TenantContext,
    TenantError,
    TenantLocks,
    TenantNotWritable,
)

RATES = ("0", "7", "10", "20")
CHAT = 999653395


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    init_db(chemin)
    store.ensure_schema(chemin)
    registry.ensure_schema(chemin)
    tenancy.migrate_to_multi_tenant(chemin)
    llm_usage.ensure_schema(chemin)

    for identifiant, alias in (
        ("xblaste", "faridrani438+xblaste@gmail.com"),
        ("fluxintelligent", "faridrani438+fluxintelligent@gmail.com"),
    ):
        registry.register_company(
            chemin, identifiant, display_name=identifiant.upper(),
            legal_name=identifiant, status=registry.ACTIVE,
            inbound_aliases=[alias],
            sheet_id=f"sheet-{identifiant}",
            drive_folder_id=f"drive-{identifiant}",
            country="MA", currency="MAD", allowed_vat_rates=RATES,
            telegram_chat_id=str(CHAT),
        )
    yield chemin
    Path(chemin).unlink(missing_ok=True)


@pytest.fixture
def deux_tenants(db_path):
    return (
        TenantContext.for_company(db_path, "xblaste"),
        TenantContext.for_company(db_path, "fluxintelligent"),
    )


# --- construction du contexte --------------------------------------------


def test_le_contexte_porte_la_destination_de_son_entreprise(deux_tenants):
    x, f = deux_tenants
    assert x.sheet_id == "sheet-xblaste"
    assert f.sheet_id == "sheet-fluxintelligent"
    assert x.drive_folder_id != f.drive_folder_id
    assert x.allowed_vat_rates == (
        Decimal("0"), Decimal("7"), Decimal("10"), Decimal("20")
    )


def test_aucun_contexte_sans_classeur_ni_drive(db_path):
    """Une ecriture ne peut pas partir vers une destination absente."""
    registry.register_company(
        db_path, "sansclasseur", status=registry.ACTIVE, telegram_chat_id=str(CHAT)
    )
    with pytest.raises(TenantNotWritable, match="sheet_id"):
        TenantContext.for_company(db_path, "sansclasseur")


def test_aucun_contexte_pour_une_entreprise_suspendue(db_path):
    registry.set_status(db_path, "xblaste", registry.SUSPENDED)
    with pytest.raises(TenantNotWritable, match="SUSPENDED"):
        TenantContext.for_company(db_path, "xblaste")


def test_aucun_contexte_pour_une_entreprise_inconnue(db_path):
    with pytest.raises(TenantError, match="inconnue"):
        TenantContext.for_company(db_path, "societeinventee")


def test_le_contexte_est_immuable(deux_tenants):
    """Un contexte construit pour XBLASTE ne peut pas etre detourne."""
    x, _ = deux_tenants
    with pytest.raises(Exception):
        x.sheet_id = "sheet-fluxintelligent"  # type: ignore[misc]


# --- la meme facture dans deux entreprises -------------------------------


def _comptabiliser(tenant, doc_key, sha, numero, message="msg-1"):
    """Reserve un document et le marque comme comptabilise."""
    tenant.claim_document(
        doc_key, gmail_message_id=message, attachment_id="att",
        file_sha256=sha, filename="facture.pdf",
    )
    store.update_document(
        tenant.db_path, doc_key, doc_type="facture_achat", numero=numero,
        stable_id=f"FA-{numero}", state=store.COMPLETED,
    )


def test_les_memes_octets_sont_comptabilises_dans_chaque_entreprise(deux_tenants):
    """Le cas central : une facture identique adressee aux deux societes."""
    x, f = deux_tenants
    sha = "a" * 64
    _comptabiliser(x, "cle-x", sha, "F2026-1101")

    # Vue de Flux Intelligent : ce fichier lui est INCONNU.
    assert f.find_by_sha256(sha) is None
    assert f.find_by_business_key("facture_achat", "F2026-1101") is None

    _comptabiliser(f, "cle-f", sha, "F2026-1101")

    # Chacune voit la sienne, et seulement la sienne.
    assert x.find_by_sha256(sha)["doc_key"] == "cle-x"
    assert f.find_by_sha256(sha)["doc_key"] == "cle-f"


def test_le_meme_numero_de_facture_coexiste_dans_deux_entreprises(deux_tenants):
    x, f = deux_tenants
    _comptabiliser(x, "cle-x", "a" * 64, "F2026-1101")
    _comptabiliser(f, "cle-f", "b" * 64, "F2026-1101")
    assert x.find_by_business_key("facture_achat", "F2026-1101")["doc_key"] == "cle-x"
    assert f.find_by_business_key("facture_achat", "F2026-1101")["doc_key"] == "cle-f"


def test_un_doublon_reste_detecte_dans_sa_propre_entreprise(deux_tenants):
    """L'isolation ne doit pas casser la deduplication interne."""
    x, _ = deux_tenants
    sha = "a" * 64
    _comptabiliser(x, "cle-x", sha, "F2026-1101")
    assert x.find_by_sha256(sha) is not None
    assert x.find_by_business_key("facture_achat", "F2026-1101") is not None


def test_une_copie_renommee_est_vue_comme_doublon_dans_le_meme_tenant(deux_tenants):
    x, f = deux_tenants
    sha = "c" * 64
    x.claim_document(
        "cle-x1", gmail_message_id="m1", attachment_id="a1",
        file_sha256=sha, filename="facture.pdf",
    )
    # Meme empreinte, autre nom, meme entreprise : jumeau ouvert.
    jumeau = x.find_open_twin(sha, exclude_key="cle-x2")
    assert jumeau is not None and jumeau["doc_key"] == "cle-x1"
    # Vue de l'autre entreprise : rien.
    assert f.find_open_twin(sha, exclude_key="cle-f2") is None


def test_un_meme_email_est_suivi_separement_par_chaque_entreprise(deux_tenants):
    """Un ZIP adresse aux deux societes : chacune garde sa propre reprise."""
    x, f = deux_tenants
    sha = "d" * 64
    x.claim_document(
        "cle-x", gmail_message_id="msg-partage", attachment_id="a",
        file_sha256=sha,
    )
    assert x.find_by_message_and_sha("msg-partage", sha)["doc_key"] == "cle-x"
    assert f.find_by_message_and_sha("msg-partage", sha) is None


# --- quarantaine ----------------------------------------------------------


def test_la_quarantaine_ne_deborde_pas_sur_l_autre_entreprise(deux_tenants):
    x, f = deux_tenants
    x.claim_document(
        "quar-x", gmail_message_id="m", attachment_id="a", file_sha256="e" * 64
    )
    store.update_document(x.db_path, "quar-x", review_row=2, state=store.NEEDS_REVIEW)
    assert [d["doc_key"] for d in x.list_quarantined()] == ["quar-x"]
    assert f.list_quarantined() == []


# --- curseurs Gmail -------------------------------------------------------


def test_chaque_entreprise_avance_son_propre_curseur(deux_tenants):
    x, f = deux_tenants
    x.cursor(1000)
    f.cursor(1000)
    x.advance_cursor(5000)
    assert x.cursor(1000)["last_internal_date"] == 5000
    assert f.cursor(1000)["last_internal_date"] == 1000, (
        "l'avancement d'une entreprise ne doit pas faire sauter "
        "des emails a une autre"
    )


# --- notifications --------------------------------------------------------


def test_les_notifications_sont_suivies_par_entreprise(deux_tenants):
    x, f = deux_tenants
    x.remember_notification("msg-partage", "signature-x")
    assert x.notification_signature("msg-partage") == "signature-x"
    assert f.notification_signature("msg-partage") == "", (
        "l'autre entreprise ne doit pas etre reduite au silence "
        "par une notification qui ne la concerne pas"
    )
    f.remember_notification("msg-partage", "signature-f")
    assert x.notification_signature("msg-partage") == "signature-x"
    assert f.notification_signature("msg-partage") == "signature-f"


# --- empreintes bancaires -------------------------------------------------


def test_une_meme_operation_bancaire_existe_dans_les_deux_entreprises(deux_tenants):
    """Deux societes peuvent avoir le meme mouvement chez la meme banque."""
    x, f = deux_tenants
    assert x.claim_bank_line("empreinte-identique", 2, "cle-x") is True
    assert f.claim_bank_line("empreinte-identique", 2, "cle-f") is True
    # Mais un rejeu dans la MEME entreprise reste refuse.
    assert x.claim_bank_line("empreinte-identique", 3, "cle-x") is False


# --- verrous et files -----------------------------------------------------


def test_le_verrou_d_une_entreprise_ne_bloque_pas_l_autre():
    verrous = TenantLocks()
    with verrous.hold("xblaste") as pris_x:
        assert pris_x
        with verrous.hold("fluxintelligent", timeout=0.1) as pris_f:
            assert pris_f, "une entreprise en cours ne doit pas bloquer les autres"


def test_deux_cycles_ne_traitent_pas_la_meme_entreprise_en_parallele():
    verrous = TenantLocks()
    with verrous.hold("xblaste"):
        with verrous.hold("xblaste", timeout=0.1) as second:
            assert not second


def test_un_verrou_refuse_un_identifiant_invalide():
    verrous = TenantLocks()
    with pytest.raises(registry.CompanyError):
        verrous.lock_for("PAS UN IDENTIFIANT")


# --- couts LLM ------------------------------------------------------------


def test_les_couts_llm_sont_ventiles_par_entreprise(db_path):
    llm_usage.record_call(
        db_path, company_id="xblaste", level="terra", model="gpt-5.6-terra",
        doc_key="cle-x", reason=llm_usage.REASON_MISSING_FIELDS,
        outcome=llm_usage.OUTCOME_REJECTED, input_tokens=1200, output_tokens=300,
        estimated_cost_usd=0.004,
    )
    llm_usage.record_call(
        db_path, company_id="xblaste", level="sol", model="gpt-5.6-sol",
        doc_key="cle-x", reason=llm_usage.REASON_UNREADABLE_IMAGE,
        outcome=llm_usage.OUTCOME_ACCEPTED, input_tokens=3000, output_tokens=500,
        estimated_cost_usd=0.02,
    )
    llm_usage.record_call(
        db_path, company_id="fluxintelligent", level="terra",
        model="gpt-5.6-terra", doc_key="cle-f", input_tokens=100,
        output_tokens=50, estimated_cost_usd=0.001,
    )

    ventilation = llm_usage.totals_by_company(db_path)
    assert ventilation["xblaste"].calls == 2
    assert ventilation["xblaste"].input_tokens == 4200
    assert round(ventilation["xblaste"].estimated_cost_usd, 4) == 0.024
    assert ventilation["fluxintelligent"].calls == 1

    assert llm_usage.levels_for_document(db_path, "xblaste", "cle-x") == (
        "terra", "sol",
    )
    assert llm_usage.calls_for_document(db_path, "fluxintelligent", "cle-x") == 0


def test_un_appel_llm_sans_entreprise_est_refuse(db_path):
    """Un cout qu'on ne sait pas imputer est un cout qu'on ne facture pas."""
    with pytest.raises(llm_usage.UsageError):
        llm_usage.record_call(
            db_path, company_id="", level="sol", model="gpt-5.6-sol"
        )


def test_le_journal_des_couts_ne_peut_pas_stocker_de_contenu(db_path):
    """Ni secret, ni base64, ni texte de document.

    Le journal des couts part dans des rapports clients : une colonne de
    contenu y ferait fuiter une facture d'une entreprise vers le rapport
    d'une autre.
    """
    llm_usage.ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage)")}
    interdits = {
        "content", "payload", "image", "image_b64", "base64", "raw_text",
        "prompt", "api_key", "token", "secret",
    }
    assert not (colonnes & interdits)


# --- absence de fuite, vue d'ensemble -------------------------------------


def test_aucune_ligne_n_appartient_a_deux_entreprises(deux_tenants):
    x, f = deux_tenants
    _comptabiliser(x, "cle-x", "a" * 64, "F-1")
    _comptabiliser(f, "cle-f", "b" * 64, "F-2")
    comptes = tenancy.company_counts(x.db_path)
    assert comptes["documents"] == {"xblaste": 1, "fluxintelligent": 1}
    assert tenancy.orphan_rows(x.db_path) == {}


def test_une_entreprise_ne_voit_jamais_les_documents_de_l_autre(deux_tenants):
    x, f = deux_tenants
    for i in range(3):
        _comptabiliser(x, f"cle-x{i}", f"{i}a" * 32, f"FX-{i}")
    docs_f = store.list_documents(f.db_path, f.chat_id)
    a_flux = [d for d in docs_f if d.get("company_id") == "fluxintelligent"]
    assert a_flux == []
