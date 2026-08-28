"""Routage multi-tenant : quelle comptabilite recoit quel email.

Le routage est la frontiere de securite du multi-tenant. Ces tests
verifient qu'il refuse par defaut et qu'aucun chemin detourne - un nom
dans le sujet, un tag pose par un inconnu, une adresse ambigue - ne peut
faire entrer une piece dans la mauvaise comptabilite.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import companies as registry
from app import routing

ALIAS_X = "faridrani438+xblaste@gmail.com"
ALIAS_F = "faridrani438+fluxintelligent@gmail.com"
ADMIN = "boukafa.ayoub@gmail.com"
INCONNU = "quelqun@exemple.com"


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    registry.ensure_schema(chemin)
    # XBLASTE : entierement configuree, donc ecrivable.
    registry.register_company(
        chemin, "xblaste", display_name="XBLASTE", legal_name="X BLASTE",
        status=registry.ACTIVE, inbound_aliases=[ALIAS_X],
        allowed_admin_senders=[ADMIN],
        sheet_id="sheet-x", drive_folder_id="drive-x",
        country="MA", currency="MAD", allowed_vat_rates=["20"],
        telegram_chat_id="999653395",
    )
    # Flux Intelligent : connue, mais en attente de ses donnees legales.
    registry.register_company(
        chemin, "fluxintelligent", display_name="Flux Intelligent",
        status=registry.PENDING_CONFIGURATION, inbound_aliases=[ALIAS_F],
        allowed_admin_senders=[ADMIN],
    )
    yield chemin
    Path(chemin).unlink(missing_ok=True)


def message(*entetes: tuple[str, str], **extra) -> dict:
    """Message Gmail minimal, avec ses en-tetes reels."""
    return {
        "payload": {"headers": [{"name": n, "value": v} for n, v in entetes]},
        **extra,
    }


# --- chemin nominal, par type d'en-tete -----------------------------------


def test_le_routage_suit_delivered_to(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", ALIAS_X), ("From", ADMIN), ("Subject", "facture"),
    ))
    assert decision.outcome == routing.ROUTED
    assert decision.company_id == "xblaste"
    assert decision.source == routing.SOURCE_DELIVERED_TO


def test_le_routage_suit_x_original_to(db_path):
    decision = routing.route_message(db_path, message(
        ("X-Original-To", ALIAS_X), ("From", ADMIN),
    ))
    assert decision.outcome == routing.ROUTED
    assert decision.source == routing.SOURCE_DELIVERED_TO


def test_le_routage_retombe_sur_to_et_cc(db_path):
    decision = routing.route_message(db_path, message(
        ("To", f"Comptabilite <{ALIAS_X}>"), ("From", ADMIN),
    ))
    assert decision.outcome == routing.ROUTED
    assert decision.source == routing.SOURCE_RECIPIENT

    decision_cc = routing.route_message(db_path, message(
        ("To", INCONNU), ("Cc", ALIAS_X), ("From", ADMIN),
    ))
    assert decision_cc.company_id == "xblaste"


def test_delivered_to_prime_sur_to(db_path):
    """L'expediteur controle `To`, jamais `Delivered-To`.

    Un email adresse en apparence a Flux Intelligent mais reellement
    livre a l'alias XBLASTE appartient a XBLASTE.
    """
    decision = routing.route_message(db_path, message(
        ("Delivered-To", ALIAS_X), ("To", ALIAS_F), ("From", ADMIN),
    ))
    assert decision.company_id == "xblaste"
    assert decision.source == routing.SOURCE_DELIVERED_TO


# --- le nom ne route jamais -----------------------------------------------


def test_un_nom_dans_le_sujet_ne_route_jamais(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", INCONNU), ("From", ADMIN),
        ("Subject", "Facture pour XBLASTE - X BLASTE SARL"),
    ))
    assert decision.outcome == routing.UNKNOWN_COMPANY
    assert decision.company_id == ""


def test_un_nom_dans_le_corps_ne_route_jamais(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", INCONNU), ("From", ADMIN),
        messageText="Merci de comptabiliser ceci pour Flux Intelligent",
    ))
    assert decision.outcome == routing.UNKNOWN_COMPANY


# --- tag administrateur ---------------------------------------------------


def test_le_tag_administrateur_route_un_expediteur_autorise(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", INCONNU), ("From", f"Ayoub <{ADMIN}>"),
        ("Subject", "[ACCOUNTING:xblaste] releve"),
    ))
    assert decision.outcome == routing.ROUTED
    assert decision.company_id == "xblaste"
    assert decision.source == routing.SOURCE_ADMIN_TAG


def test_le_tag_d_un_expediteur_non_autorise_est_ignore(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", INCONNU), ("From", INCONNU),
        ("Subject", "[ACCOUNTING:xblaste] facture"),
    ))
    assert decision.outcome == routing.UNKNOWN_COMPANY
    assert "non administrateur" in decision.reason


def test_deux_tags_differents_ne_valent_aucun_tag(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", INCONNU), ("From", ADMIN),
        ("Subject", "[ACCOUNTING:xblaste] [ACCOUNTING:fluxintelligent]"),
    ))
    assert decision.outcome == routing.UNKNOWN_COMPANY


# --- conflits et ambiguites ----------------------------------------------


def test_alias_et_tag_contradictoires_partent_en_quarantaine(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", ALIAS_X), ("From", ADMIN),
        ("Subject", "[ACCOUNTING:fluxintelligent] facture"),
    ))
    assert decision.outcome == routing.CONFLICT
    assert decision.quarantined
    assert not decision.accepted
    assert set(decision.candidates) == {"xblaste", "fluxintelligent"}


def test_deux_alias_de_livraison_partent_en_quarantaine(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", ALIAS_X), ("Delivered-To", ALIAS_F), ("From", ADMIN),
    ))
    assert decision.outcome == routing.AMBIGUOUS
    assert decision.quarantined


def test_deux_alias_dans_to_partent_en_quarantaine(db_path):
    decision = routing.route_message(db_path, message(
        ("To", f"{ALIAS_X}, {ALIAS_F}"), ("From", ADMIN),
    ))
    assert decision.outcome == routing.AMBIGUOUS


# --- societes non ecrivables ---------------------------------------------


def test_une_societe_en_attente_de_configuration_n_ecrit_pas(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", ALIAS_F), ("From", ADMIN),
    ))
    assert decision.outcome == routing.NOT_WRITABLE
    assert decision.company_id == "fluxintelligent"
    assert not decision.accepted
    assert "sheet_id" in decision.reason


def test_une_societe_desactivee_n_ecrit_pas(db_path):
    registry.set_status(db_path, "xblaste", registry.DISABLED)
    decision = routing.route_message(db_path, message(
        ("Delivered-To", ALIAS_X), ("From", ADMIN),
    ))
    assert decision.outcome == routing.NOT_WRITABLE
    assert "DISABLED" in decision.reason


def test_une_societe_suspendue_n_ecrit_pas(db_path):
    registry.set_status(db_path, "xblaste", registry.SUSPENDED)
    decision = routing.route_message(db_path, message(
        ("Delivered-To", ALIAS_X), ("From", ADMIN),
    ))
    assert decision.outcome == routing.NOT_WRITABLE


# --- societe inconnue -----------------------------------------------------


def test_une_societe_inconnue_n_est_jamais_creee(db_path):
    avant = {c.company_id for c in registry.list_companies(db_path)}
    decision = routing.route_message(db_path, message(
        ("Delivered-To", "faridrani438+societeinventee@gmail.com"),
        ("From", INCONNU), ("Subject", "Facture SOCIETE INVENTEE SARL"),
    ))
    assert decision.outcome == routing.UNKNOWN_COMPANY
    apres = {c.company_id for c in registry.list_companies(db_path)}
    assert apres == avant, "aucune entreprise ne doit naitre d'un email entrant"


def test_un_email_sans_aucun_destinataire_connu_est_refuse(db_path):
    decision = routing.route_message(db_path, message(("From", ADMIN)))
    assert decision.outcome == routing.UNKNOWN_COMPANY
    assert not decision.accepted


# --- casse et formes d'en-tetes ------------------------------------------


def test_la_casse_des_adresses_n_empeche_pas_le_routage(db_path):
    decision = routing.route_message(db_path, message(
        ("Delivered-To", ALIAS_X.upper()), ("From", ADMIN),
    ))
    assert decision.company_id == "xblaste"


def test_les_emails_de_verification_ne_routent_nulle_part(db_path):
    """Le lot de verification ne doit atteindre aucune comptabilite.

    Il arrive sur `+accounting-verif`, qui n'est l'alias d'aucune
    entreprise : le registre suffit a le refuser, meme si la requete
    Gmail venait a le laisser passer.
    """
    decision = routing.route_message(db_path, message(
        ("Delivered-To", "faridrani438+accounting-verif@gmail.com"),
        ("From", ADMIN),
        ("Subject", "[ACCOUNTING-VERIF-2026-08] DOSSIER-COMPTABLE-AOUT-01"),
    ))
    assert decision.outcome == routing.UNKNOWN_COMPANY
    assert not decision.accepted
