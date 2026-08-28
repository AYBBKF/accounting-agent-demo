"""Le repartiteur multi-entreprises : un sondage, N comptabilites.

Ces tests ne verifient pas des intentions mais des CHEMINS : quel email
est parti chez qui, sur quel en-tete, et surtout ce qui n'est PAS arrive
chez l'autre. Le pipeline complet n'est pas rejoue ici - il l'est dans la
validation E2E - ce qui compte a ce niveau est la frontiere.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import companies as registry
from app import doc_store as store
from app import llm_usage
from app import routing
from app import tenancy
from app import tenant_worker as tw
from app.db import init_db
from app.mail_worker import MailSummary
from app.tenant_context import TenantContext, TenantLocks

XBLASTE_ALIAS = "faridrani438+xblaste@gmail.com"
FLUX_ALIAS = "faridrani438+fluxintelligent@gmail.com"
CHAT = 999653395
# Horloge fixe : le curseur s'initialise a la valeur qu'on lui donne.
NOW = 1_787_000_000
RECENT = 1_787_872_375

QUERY = (
    "in:inbox has:attachment "
    "{filename:pdf filename:zip filename:png filename:jpg filename:jpeg} "
    '-subject:"ACCOUNTING-VERIF"'
)


@pytest.fixture
def db_path():
    chemin = tempfile.mktemp(suffix=".db")
    init_db(chemin)
    store.ensure_schema(chemin)
    registry.ensure_schema(chemin)
    tenancy.migrate_to_multi_tenant(chemin)
    llm_usage.ensure_schema(chemin)
    for identifiant, alias in (
        ("xblaste", XBLASTE_ALIAS),
        ("fluxintelligent", FLUX_ALIAS),
    ):
        registry.register_company(
            chemin, identifiant, display_name=identifiant.upper(),
            legal_name=identifiant, status=registry.ACTIVE,
            inbound_aliases=[alias],
            sheet_id=f"sheet-{identifiant}",
            drive_folder_id=f"drive-{identifiant}",
            country="MA", currency="MAD",
            allowed_vat_rates=("0", "7", "10", "20"),
            telegram_chat_id=str(CHAT),
        )
    yield chemin
    Path(chemin).unlink(missing_ok=True)


# --- doublures ------------------------------------------------------------


class FauxWorker:
    """Worker d'UNE entreprise, qui note ce qu'on lui confie."""

    def __init__(self, company_id: str, sheet_id: str, drive: str) -> None:
        self.company_id = company_id
        self.sheet_id = sheet_id
        self.drive_folder = drive
        self.traites: list[str] = []

    def process_message(self, message_id: str) -> tuple[MailSummary, int]:
        self.traites.append(message_id)
        return (
            MailSummary(message_id=message_id, subject="", sender=""),
            RECENT,
        )


def _message(message_id: str, delivered_to: str = "", to: str = "",
             subject: str = "Facture") -> dict:
    entetes = []
    if delivered_to:
        entetes.append({"name": "Delivered-To", "value": delivered_to})
    if to:
        entetes.append({"name": "To", "value": to})
    entetes.append({"name": "Subject", "value": subject})
    entetes.append({"name": "From", "value": "fournisseur@exemple.ma"})
    return {
        "messageId": message_id,
        "subject": subject,
        "sender": "fournisseur@exemple.ma",
        "payload": {"headers": entetes},
    }


class FauxSondeur:
    """Remplace le sondeur Gmail : rend des messages, sans reseau."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = {m["messageId"]: m for m in messages}
        self.requetes: list[str] = []

    def execute(self, slug: str, arguments: dict) -> dict:
        assert slug == "GMAIL_FETCH_EMAILS"
        self.requetes.append(arguments["query"])
        return {"messages": [{"messageId": mid} for mid in self._messages]}

    def fetch_message(self, message_id: str) -> dict:
        return self._messages[message_id]


def _worker(db_path, messages, **kwargs):
    faux: dict[str, FauxWorker] = {}

    def fabrique(tenant: TenantContext) -> FauxWorker:
        faux[tenant.company_id] = FauxWorker(
            tenant.company_id, tenant.sheet_id, tenant.drive_folder_id
        )
        return faux[tenant.company_id]

    moteur = tw.TenantWorker(
        api_key="cle", chat_id=CHAT, db_path=db_path, query=QUERY,
        worker_factory=fabrique, **kwargs,
    )
    moteur._probe = FauxSondeur(messages)
    return moteur, faux


# --- routage et attribution ----------------------------------------------


def test_deux_entreprises_traitees_dans_le_meme_processus(db_path):
    moteur, faux = _worker(db_path, [
        _message("m-x", delivered_to=XBLASTE_ALIAS),
        _message("m-f", delivered_to=FLUX_ALIAS),
    ])
    rapport = moteur.process_once()

    assert rapport.seen == 2
    assert rapport.routed == {"xblaste": 1, "fluxintelligent": 1}
    assert faux["xblaste"].traites == ["m-x"]
    assert faux["fluxintelligent"].traites == ["m-f"]


def test_aucun_email_ne_traverse_la_frontiere(db_path):
    """La preuve negative : l'autre entreprise n'a RIEN vu."""
    moteur, faux = _worker(db_path, [_message("m-x", delivered_to=XBLASTE_ALIAS)])
    moteur.process_once()

    assert faux["xblaste"].traites == ["m-x"]
    assert "fluxintelligent" not in faux, (
        "aucun worker ne doit meme etre construit pour l'entreprise non concernee"
    )


def test_chaque_worker_ecrit_dans_son_propre_classeur(db_path):
    moteur, faux = _worker(db_path, [
        _message("m-x", delivered_to=XBLASTE_ALIAS),
        _message("m-f", delivered_to=FLUX_ALIAS),
    ])
    moteur.process_once()

    assert faux["xblaste"].sheet_id == "sheet-xblaste"
    assert faux["fluxintelligent"].sheet_id == "sheet-fluxintelligent"
    assert faux["xblaste"].drive_folder == "drive-xblaste"
    assert faux["fluxintelligent"].drive_folder == "drive-fluxintelligent"


def test_l_entete_de_livraison_prime_sur_le_destinataire(db_path):
    """`To` est controle par l'expediteur ; `Delivered-To` non."""
    moteur, faux = _worker(db_path, [
        _message("m-1", delivered_to=XBLASTE_ALIAS, to=FLUX_ALIAS),
    ])
    rapport = moteur.process_once()

    assert rapport.routed == {"xblaste": 1}
    assert rapport.emails[0].source == routing.SOURCE_DELIVERED_TO
    assert "fluxintelligent" not in faux


def test_un_nom_d_entreprise_dans_le_sujet_ne_route_rien(db_path):
    """Sinon n'importe qui ecrirait dans la comptabilite de son choix."""
    moteur, faux = _worker(db_path, [
        _message("m-1", subject="Facture pour FLUXINTELLIGENT et xblaste"),
    ])
    rapport = moteur.process_once()

    assert rapport.routed == {}
    assert faux == {}
    assert rapport.quarantined[0].outcome == routing.UNKNOWN_COMPANY


def test_un_email_non_routable_ne_coute_rien(db_path):
    """Ni telechargement, ni extraction, ni appel LLM : rien n'est fait."""
    moteur, faux = _worker(db_path, [_message("m-verif", to="autre@gmail.com")])
    rapport = moteur.process_once()

    assert rapport.quarantined and not rapport.quarantined[0].processed
    assert faux == {}
    assert llm_usage.totals_by_company(db_path) == {}


def test_un_alias_ambigu_est_refuse_plutot_que_devine(db_path):
    moteur, _ = _worker(db_path, [
        _message("m-1", delivered_to=f"{XBLASTE_ALIAS}, {FLUX_ALIAS}"),
    ])
    rapport = moteur.process_once()

    assert rapport.emails[0].outcome == routing.AMBIGUOUS
    assert not rapport.emails[0].processed


# --- entreprises non ecrivables -------------------------------------------


def test_une_entreprise_suspendue_met_l_email_en_quarantaine(db_path):
    registry.set_status(db_path, "fluxintelligent", registry.SUSPENDED)
    moteur, faux = _worker(db_path, [_message("m-f", delivered_to=FLUX_ALIAS)])
    rapport = moteur.process_once()

    assert rapport.emails[0].outcome == routing.NOT_WRITABLE
    assert not rapport.emails[0].processed
    assert faux == {}


def test_une_entreprise_sans_classeur_ne_peut_pas_ecrire(db_path):
    registry.update_company(db_path, "fluxintelligent", sheet_id="")
    moteur, faux = _worker(db_path, [_message("m-f", delivered_to=FLUX_ALIAS)])
    rapport = moteur.process_once()

    assert rapport.emails[0].outcome == routing.NOT_WRITABLE
    assert faux == {}


def test_une_entreprise_en_attente_de_configuration_ne_traite_rien(db_path):
    """Cas Flux Intelligent tant que ses donnees legales manquent."""
    registry.set_status(db_path, "fluxintelligent", registry.PENDING_CONFIGURATION)
    moteur, faux = _worker(db_path, [
        _message("m-f", delivered_to=FLUX_ALIAS),
        _message("m-x", delivered_to=XBLASTE_ALIAS),
    ])
    rapport = moteur.process_once()

    assert rapport.routed.get("xblaste") == 1
    assert faux["xblaste"].traites == ["m-x"]
    assert not [e for e in rapport.emails if e.processed and e.company_id == "fluxintelligent"]


# --- curseurs --------------------------------------------------------------


def test_chaque_entreprise_avance_son_propre_curseur(db_path):
    """Seule l'entreprise a qui l'email a ete attribue avance."""
    xbl = TenantContext.for_company(db_path, "xblaste")
    flux = TenantContext.for_company(db_path, "fluxintelligent")
    xbl.cursor(NOW)
    avant = flux.cursor(NOW)["last_internal_date"]

    moteur, _ = _worker(db_path, [_message("m-x", delivered_to=XBLASTE_ALIAS)])
    moteur.process_once()

    assert flux.cursor(NOW)["last_internal_date"] == avant, (
        "le curseur de l'entreprise non concernee ne doit pas bouger"
    )
    assert xbl.cursor(NOW)["last_internal_date"] == RECENT


def test_un_curseur_ne_recule_jamais_a_cause_d_un_vieil_email(db_path):
    """Un curseur qui recule ferait retraiter d'anciens emails."""
    xbl = TenantContext.for_company(db_path, "xblaste")
    xbl.cursor(NOW)
    TenantContext.for_company(db_path, "fluxintelligent").cursor(NOW)
    xbl.advance_cursor(RECENT)
    moteur, _ = _worker(db_path, [_message("m-x", delivered_to=XBLASTE_ALIAS)])
    worker = moteur.worker_for("xblaste")
    worker.process_message = lambda mid: (
        MailSummary(message_id=mid, subject="", sender=""), NOW - 999_999
    )
    moteur.process_once()

    assert xbl.cursor(NOW)["last_internal_date"] == RECENT


def test_la_borne_gmail_est_le_plus_ancien_des_curseurs(db_path):
    """Prendre le plus recent ferait sauter une comptabilite en retard."""
    xbl = TenantContext.for_company(db_path, "xblaste")
    flux = TenantContext.for_company(db_path, "fluxintelligent")
    xbl.cursor(NOW)
    flux.cursor(NOW)
    xbl.rewind_cursor(30 * 86400)          # XBLASTE a un mois de retard
    moteur, _ = _worker(db_path, [])

    borne = moteur.oldest_floor()
    assert borne == store.query_floor(xbl.cursor(NOW))
    assert borne < store.query_floor(flux.cursor(NOW)), (
        "la borne doit suivre l'entreprise la plus en retard"
    )


def test_un_reprocess_ne_recule_que_le_curseur_demande(db_path):
    """Regression : le recul n'etait PAS limite a une entreprise.

    Les deux entreprises partagent le meme canal Telegram, donc le meme
    `chat_id`. Un `/reprocess` demande pour l'une reculait le curseur de
    l'autre, qui relisait alors des semaines d'emails sans raison.
    """
    xbl = TenantContext.for_company(db_path, "xblaste")
    flux = TenantContext.for_company(db_path, "fluxintelligent")
    xbl.cursor(NOW)
    intact = flux.cursor(NOW)["last_internal_date"]

    xbl.rewind_cursor(7 * 86400)

    assert xbl.cursor(NOW)["last_internal_date"] == NOW - 7 * 86400
    assert flux.cursor(NOW)["last_internal_date"] == intact


# --- requete Gmail ---------------------------------------------------------


def test_la_requete_de_production_est_chargee_telle_quelle(db_path):
    moteur, _ = _worker(db_path, [_message("m-x", delivered_to=XBLASTE_ALIAS)])
    moteur.process_once()

    envoyee = moteur.probe.requetes[0]
    assert envoyee.startswith(QUERY), envoyee
    assert '-subject:"ACCOUNTING-VERIF"' in envoyee
    assert "filename:png" in envoyee and "filename:jpeg" in envoyee


def test_une_requete_vide_refuse_de_demarrer(db_path):
    """Un fallback silencieux vers une requete large est interdit."""
    with pytest.raises(ValueError, match="requete"):
        tw.TenantWorker(api_key="cle", chat_id=CHAT, db_path=db_path, query="")
    with pytest.raises(ValueError):
        tw.TenantWorker(api_key="cle", chat_id=CHAT, db_path=db_path, query="   ")


# --- verrous et files ------------------------------------------------------


def test_une_entreprise_deja_en_cours_est_reportee_pas_doublee(db_path):
    verrous = TenantLocks()
    verrous.lock_for("xblaste").acquire()
    moteur, faux = _worker(
        db_path,
        [_message("m-x", delivered_to=XBLASTE_ALIAS),
         _message("m-f", delivered_to=FLUX_ALIAS)],
        locks=verrous,
    )
    try:
        rapport = moteur.process_once()
    finally:
        verrous.lock_for("xblaste").release()

    assert "xblaste" in rapport.skipped_busy
    assert "xblaste" not in faux or faux["xblaste"].traites == []
    # L'entreprise voisine n'est pas retardee par le verrou de l'autre.
    assert faux["fluxintelligent"].traites == ["m-f"]


def test_le_verrou_d_une_entreprise_ne_bloque_pas_les_autres(db_path):
    verrous = TenantLocks()
    verrous.lock_for("fluxintelligent").acquire()
    moteur, faux = _worker(
        db_path, [_message("m-x", delivered_to=XBLASTE_ALIAS)], locks=verrous
    )
    try:
        moteur.process_once()
    finally:
        verrous.lock_for("fluxintelligent").release()

    assert faux["xblaste"].traites == ["m-x"]


# --- reutilisation et coherence -------------------------------------------


def test_le_worker_d_une_entreprise_est_reutilise(db_path):
    """Reconstruire un worker par email relancerait les caches a chaque tour."""
    moteur, faux = _worker(db_path, [_message("m-x", delivered_to=XBLASTE_ALIAS)])
    premier = moteur.worker_for("xblaste")
    second = moteur.worker_for("xblaste")
    assert premier is second


def test_un_worker_construit_pour_la_mauvaise_entreprise_est_refuse(db_path):
    """Garde-fou structurel : une fabrique fautive ne passe pas."""
    def fabrique_fautive(tenant):
        return FauxWorker("uneautre", tenant.sheet_id, tenant.drive_folder_id)

    moteur = tw.TenantWorker(
        api_key="cle", chat_id=CHAT, db_path=db_path, query=QUERY,
        worker_factory=fabrique_fautive,
    )
    with pytest.raises(Exception, match="mauvaise entreprise"):
        moteur.worker_for("xblaste")


def test_les_entreprises_touchees_sont_tracables(db_path):
    moteur, _ = _worker(db_path, [
        _message("m-x", delivered_to=XBLASTE_ALIAS),
        _message("m-verif", to="inconnu@gmail.com"),
    ])
    rapport = moteur.process_once()
    assert tw.companies_touched([rapport]) == ("xblaste",)
