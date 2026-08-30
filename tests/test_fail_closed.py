"""Fail-closed : le multi-entreprises demande demarre sainement ou pas du tout.

Le pire mode de panne d'un agent comptable multi-clients n'est pas
l'arret : c'est le repli silencieux. Un processus qui, faute de
configuration, retomberait sur le worker mono-entreprise continuerait a
servir XBLASTE, avancerait le curseur Gmail commun, et laisserait croire
que tous les clients sont servis - alors que leurs emails seraient lus
puis perdus. Ces tests verrouillent le contrat : quand l'exploitant a
demande le multi-entreprises, tout echec de preparation est TERMINAL.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from app import bot as botmod
from app import companies as registry
from app import doc_store as store
from app import multitenant_runtime as runtime
from app.db import init_db

XBLASTE_ALIAS = "faridrani438+xblaste@gmail.com"


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    init_db(chemin)
    store.ensure_schema(chemin)
    yield chemin
    Path(chemin).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _etat_fatal_propre():
    botmod._fatal_startup_error = None
    yield
    botmod._fatal_startup_error = None


def _declaration_complete():
    return json.dumps([{
        "company_id": "xblaste",
        "inbound_aliases": [XBLASTE_ALIAS],
        "legal_name": "X BLASTE",
        "country": "MA", "currency": "MAD",
        "allowed_vat_rates": ["0", "7", "10", "20"],
        "telegram_chat_id": "999653395",
        "sheet_id": "sheet-x", "drive_folder_id": "drive-x",
        "status": registry.ACTIVE,
    }])


# --- niveau runtime : prepare_or_fail --------------------------------------


def test_une_configuration_saine_demarre(db_path):
    rapport = runtime.prepare_or_fail(
        db_path, companies_json=_declaration_complete()
    )
    assert rapport.writable == ("xblaste",)


def test_un_json_illisible_est_terminal(db_path):
    with pytest.raises(runtime.MultiTenantStartupError):
        runtime.prepare_or_fail(db_path, companies_json="{casse")


def test_une_declaration_vide_est_terminale(db_path):
    """Multi-tenant actif sans entreprise = rien a servir : on refuse."""
    with pytest.raises(runtime.MultiTenantStartupError, match="aucune"):
        runtime.prepare_or_fail(db_path, companies_json="")


def test_aucune_entreprise_ecrivable_est_terminal(db_path):
    """Des entreprises declarees mais aucune complete : demarrer serait
    lire les emails de clients qu'on ne peut pas servir."""
    incomplete = json.dumps([{
        "company_id": "xblaste", "inbound_aliases": [XBLASTE_ALIAS],
    }])
    with pytest.raises(runtime.MultiTenantStartupError, match="ecrivable"):
        runtime.prepare_or_fail(db_path, companies_json=incomplete)


def test_une_panne_interne_devient_terminale(db_path, monkeypatch):
    """Peu importe la panne : migration, registre, disque - meme issue."""
    def migration_cassee(*a, **k):
        raise sqlite_error

    sqlite_error = RuntimeError("disque en lecture seule")
    monkeypatch.setattr(runtime.tenancy, "migrate_to_multi_tenant", migration_cassee)
    with pytest.raises(runtime.MultiTenantStartupError, match="lecture seule"):
        runtime.prepare_or_fail(db_path, companies_json=_declaration_complete())


def test_l_erreur_terminale_est_structuree(db_path):
    """Le message doit permettre le diagnostic sans rouvrir le code."""
    try:
        runtime.prepare_or_fail(db_path, companies_json="{casse")
    except runtime.MultiTenantStartupError as exc:
        assert "COMPANIES_JSON" in str(exc)
    else:  # pragma: no cover
        pytest.fail("l'erreur terminale n'a pas ete levee")


# --- niveau processus : la boucle Gmail ------------------------------------


class _FauxBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, texte):
        self.messages.append((chat_id, texte))


def _lancer_boucle(monkeypatch, db_path, *, companies_json):
    """Lance _gmail_watch_loop avec le multi-tenant actif et une
    configuration donnee ; rend (bot, exception eventuelle)."""
    monkeypatch.setattr(botmod.settings, "multi_tenant_enabled", True)
    monkeypatch.setattr(botmod.settings, "companies_json", companies_json)
    monkeypatch.setattr(botmod.settings, "template_sheet_id", "")
    monkeypatch.setattr(botmod.settings, "db_path", db_path)
    monkeypatch.setattr(botmod.settings, "gmail_watch_enabled", True)
    monkeypatch.setattr(botmod.settings, "gmail_watch_chat_id", 999653395)
    # Le worker mono-entreprise NE DOIT PAS etre touche : on le rend
    # explosif pour le prouver.
    def interdit(*a, **k):  # pragma: no cover - ne doit jamais etre appele
        raise AssertionError("REPLI MONO-ENTREPRISE INTERDIT")

    monkeypatch.setattr(botmod.mail_worker, "process_once", interdit)
    monkeypatch.setattr(
        type(botmod.mail_worker), "is_configured",
        property(lambda self: True),
    )

    bot = _FauxBot()
    asyncio.run(asyncio.wait_for(botmod._gmail_watch_loop(bot), timeout=10))
    return bot


def test_le_processus_s_arrete_au_lieu_de_retomber_en_mono(db_path, monkeypatch):
    """LE test central : configuration cassee => la boucle rend la main
    sans avoir touche au worker mono-entreprise ni traite un email."""
    bot = _lancer_boucle(monkeypatch, db_path, companies_json="{casse")

    assert botmod._fatal_startup_error is not None
    assert "multi-entreprises" in botmod._fatal_startup_error


def test_l_administrateur_est_alerte_du_fail_closed(db_path, monkeypatch):
    bot = _lancer_boucle(monkeypatch, db_path, companies_json="")
    assert bot.messages, "l'exploitant doit etre prevenu de l'arret"
    chat_id, texte = bot.messages[0]
    assert chat_id == 999653395
    assert "fail-closed" in texte or "ARRETE" in texte


def test_aucun_email_traite_et_aucun_curseur_avance(db_path, monkeypatch):
    """L'arret fail-closed ne laisse AUCUNE trace d'activite."""
    import sqlite3

    _lancer_boucle(monkeypatch, db_path, companies_json="{casse")
    with sqlite3.connect(db_path) as conn:
        documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        curseurs = conn.execute(
            "SELECT COUNT(*) FROM gmail_sync_state"
        ).fetchone()[0]
    assert documents == 0
    assert curseurs == 0


def test_le_heartbeat_cesse_en_fail_closed(tmp_path, monkeypatch):
    """Le conteneur DOIT devenir unhealthy : heartbeat gele."""
    coeur = tmp_path / "heartbeat"
    monkeypatch.setattr(botmod, "HEARTBEAT_PATH", coeur)
    botmod._declare_fatal("test")

    async def un_tour():
        tache = asyncio.create_task(botmod._heartbeat_loop())
        await asyncio.sleep(0.05)
        tache.cancel()
        try:
            await tache
        except asyncio.CancelledError:
            pass

    asyncio.run(un_tour())
    assert not coeur.exists(), "aucun heartbeat ne doit etre ecrit en fail-closed"


def test_le_heartbeat_bat_normalement_sans_panne(tmp_path, monkeypatch):
    coeur = tmp_path / "heartbeat"
    monkeypatch.setattr(botmod, "HEARTBEAT_PATH", coeur)

    async def un_tour():
        tache = asyncio.create_task(botmod._heartbeat_loop())
        await asyncio.sleep(0.05)
        tache.cancel()
        try:
            await tache
        except asyncio.CancelledError:
            pass

    asyncio.run(un_tour())
    assert coeur.exists()


def test_le_mode_mono_reste_disponible_quand_il_est_choisi(db_path, monkeypatch):
    """MULTI_TENANT_ENABLED=false : le comportement historique est intact.

    La boucle mono-entreprise tourne a l'infini ; on se contente de
    verifier qu'elle DEMARRE (premier process_once atteint) au lieu de
    s'arreter en fail-closed.
    """
    monkeypatch.setattr(botmod.settings, "multi_tenant_enabled", False)
    monkeypatch.setattr(botmod.settings, "gmail_watch_enabled", True)
    monkeypatch.setattr(
        type(botmod.mail_worker), "is_configured",
        property(lambda self: True),
    )

    atteint = {"oui": False}

    def premier_cycle():
        atteint["oui"] = True
        raise KeyboardInterrupt  # sortir de la boucle infinie du test

    monkeypatch.setattr(botmod.mail_worker, "process_once", premier_cycle)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(asyncio.wait_for(botmod._gmail_watch_loop(_FauxBot()), timeout=10))

    assert atteint["oui"], "le mode mono-entreprise choisi doit demarrer"
    assert botmod._fatal_startup_error is None
