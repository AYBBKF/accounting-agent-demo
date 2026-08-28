"""Registre central des entreprises : ce qu'il autorise, ce qu'il refuse.

Le registre est la seule autorite qui fait exister une entreprise. Ces
tests verrouillent les regles qui empechent une comptabilite d'en
contaminer une autre.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from app import companies as registry


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    registry.ensure_schema(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def test_une_entreprise_inscrite_est_relue_a_l_identique(db_path):
    registry.register_company(
        db_path, "xblaste",
        display_name="XBLASTE", legal_name="X BLASTE SARL",
        status=registry.ACTIVE,
        inbound_aliases=["faridrani438+xblaste@gmail.com"],
        sheet_id="sheet-x", drive_folder_id="drive-x",
        country="MA", currency="mad",
        allowed_vat_rates=["0", "7", "10", "20"],
        telegram_chat_id="999653395",
        template_version="V2",
    )
    entreprise = registry.get_company(db_path, "xblaste")
    assert entreprise is not None
    assert entreprise.legal_name == "X BLASTE SARL"
    assert entreprise.currency == "MAD"
    assert entreprise.allowed_vat_rates == (
        Decimal("0"), Decimal("7"), Decimal("10"), Decimal("20"),
    )
    assert entreprise.can_write


def test_l_identifiant_est_normalise_et_contraint(db_path):
    for invalide in ["", "X", "avec espace", "MAJUSCULES!", "a" * 64]:
        with pytest.raises(registry.CompanyError):
            registry.register_company(db_path, invalide)


def test_deux_entreprises_ne_peuvent_pas_partager_un_alias(db_path):
    alias = "faridrani438+xblaste@gmail.com"
    registry.register_company(db_path, "xblaste", inbound_aliases=[alias])
    with pytest.raises(registry.CompanyError, match="appartient deja"):
        registry.register_company(db_path, "fluxintelligent", inbound_aliases=[alias])


def test_rattacher_un_alias_deja_pris_est_refuse(db_path):
    alias = "faridrani438+xblaste@gmail.com"
    registry.register_company(db_path, "xblaste", inbound_aliases=[alias])
    registry.register_company(db_path, "fluxintelligent")
    with pytest.raises(registry.CompanyError, match="appartient deja"):
        registry.add_alias(db_path, "fluxintelligent", alias)


def test_l_alias_est_compare_sans_tenir_compte_de_la_casse(db_path):
    registry.register_company(
        db_path, "xblaste", inbound_aliases=["Faridrani438+XBLASTE@Gmail.com"]
    )
    trouvee = registry.company_for_alias(db_path, "faridrani438+xblaste@gmail.com")
    assert trouvee is not None and trouvee.company_id == "xblaste"


def test_une_entreprise_en_attente_de_configuration_n_ecrit_pas(db_path):
    entreprise = registry.register_company(
        db_path, "fluxintelligent",
        display_name="Flux Intelligent",
        status=registry.PENDING_CONFIGURATION,
        inbound_aliases=["faridrani438+fluxintelligent@gmail.com"],
    )
    assert not entreprise.can_write
    assert "legal_name" in entreprise.missing_for_activation
    assert "sheet_id" in entreprise.missing_for_activation


def test_une_entreprise_active_sans_classeur_n_ecrit_pas(db_path):
    """ACTIVE ne suffit pas : sans classeur ni Drive, on ecrirait dans le vide."""
    registry.register_company(db_path, "v2-smoke", status=registry.ACTIVE)
    entreprise = registry.get_company(db_path, "v2-smoke")
    assert entreprise is not None
    assert entreprise.status == registry.ACTIVE
    assert not entreprise.can_write


@pytest.mark.parametrize(
    "statut", [registry.SUSPENDED, registry.DISABLED, registry.PENDING_CONFIGURATION]
)
def test_seul_active_autorise_l_ecriture(db_path, statut):
    registry.register_company(
        db_path, "v2-smoke", status=registry.ACTIVE,
        sheet_id="s", drive_folder_id="d",
    )
    assert registry.get_company(db_path, "v2-smoke").can_write
    registry.set_status(db_path, "v2-smoke", statut)
    assert not registry.get_company(db_path, "v2-smoke").can_write


def test_la_date_de_premiere_activation_ne_bouge_plus(db_path):
    registry.register_company(db_path, "v2-smoke", status=registry.ACTIVE)
    premiere = registry.get_company(db_path, "v2-smoke").activated_at
    assert premiere
    registry.set_status(db_path, "v2-smoke", registry.SUSPENDED)
    registry.set_status(db_path, "v2-smoke", registry.ACTIVE)
    assert registry.get_company(db_path, "v2-smoke").activated_at == premiere


def test_un_statut_inconnu_est_refuse(db_path):
    registry.register_company(db_path, "v2-smoke")
    with pytest.raises(registry.CompanyError):
        registry.set_status(db_path, "v2-smoke", "PRESQUE_ACTIVE")


def test_le_registre_ne_stocke_aucun_secret(db_path):
    """Aucune colonne du registre ne doit pouvoir accueillir un secret.

    Le registre part dans les journaux, les rapports et les sauvegardes :
    une cle qui s'y glisserait fuirait partout a la fois.
    """
    import sqlite3

    registry.register_company(db_path, "xblaste")
    with sqlite3.connect(db_path) as conn:
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(companies)")}
    interdits = {
        "api_key", "token", "secret", "password", "client_secret",
        "openai_api_key", "composio_api_key", "telegram_bot_token",
    }
    assert not (colonnes & interdits)
    assert not any(
        motif in colonne for colonne in colonnes
        for motif in ("secret", "token", "password", "api_key")
    )


def test_les_champs_non_modifiables_sont_refuses(db_path):
    registry.register_company(db_path, "xblaste")
    # Le statut a son propre chemin (`set_status`), qui tient la date
    # d'activation a jour : le laisser passer ici la desynchroniserait.
    with pytest.raises(registry.CompanyError, match="non modifiables"):
        registry.update_company(db_path, "xblaste", status=registry.ACTIVE)
    # Les alias engagent la table d'unicite : les modifier en aveugle
    # pourrait voler l'adresse d'une autre comptabilite.
    with pytest.raises(registry.CompanyError, match="non modifiables"):
        registry.update_company(db_path, "xblaste", inbound_aliases=["a@b.c"])
    with pytest.raises(registry.CompanyError, match="non modifiables"):
        registry.update_company(db_path, "xblaste", created_at="2020-01-01")


def test_l_identifiant_est_structurellement_immuable(db_path):
    """`company_id` est un parametre positionnel : il ne peut pas etre
    reecrit par une mise a jour, meme par erreur de frappe."""
    registry.register_company(db_path, "xblaste")
    with pytest.raises(TypeError):
        registry.update_company(db_path, "xblaste", company_id="autre")


def test_le_bootstrap_peut_completer_puis_activer(db_path):
    registry.register_company(
        db_path, "v2-smoke",
        status=registry.PENDING_CONFIGURATION,
        inbound_aliases=["faridrani438+v2smoke@gmail.com"],
        legal_name="Tenant de fumee", country="MA", currency="MAD",
        allowed_vat_rates=["20"], telegram_chat_id="999653395",
    )
    registry.update_company(
        db_path, "v2-smoke", sheet_id="sheet-smoke", drive_folder_id="drive-smoke",
        template_version="V2", config_validation_status=registry.CONFIG_OK,
    )
    entreprise = registry.set_status(db_path, "v2-smoke", registry.ACTIVE)
    assert entreprise.missing_for_activation == ()
    assert entreprise.can_write
