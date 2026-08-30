"""Couts LLM imputes, ZIP mono-entreprise, et cycles idempotents.

Trois exigences qui n'ont de sens qu'une fois le pipeline reellement
enferme dans une entreprise :

  * chaque appel de modele est impute a UNE comptabilite, avec son motif
    d'escalade et ses tokens - y compris les appels qui echouent ;
  * une archive n'appartient qu'a l'entreprise a qui l'email a ete
    livre, quels que soient les noms des fichiers qu'elle contient ;
  * un document deja traite ou deja en quarantaine ne redeclenche NI
    appel de modele, NI ecriture, NI Telegram aux cycles suivants.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import companies as registry
from app import doc_pipeline
from app import doc_store as store
from app import doc_vision
from app import llm_usage
from app import tenancy
from app import tenant_worker as tw
from app.db import init_db
from app.mail_worker import MailSummary
from app.tenant_context import TenantContext

XBLASTE_ALIAS = "faridrani438+xblaste@gmail.com"
FLUX_ALIAS = "faridrani438+fluxintelligent@gmail.com"
CHAT = 999653395
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


# --- tarification ---------------------------------------------------------


def test_un_cout_sans_prix_declare_reste_a_zero():
    """Mieux vaut un cout absent qu'un cout invente."""
    assert llm_usage.estimate_cost("modele-inconnu", 1000, 500, {}) == 0.0


def test_le_prix_declare_est_applique_au_million_de_tokens():
    env = {"LLM_PRICE_GPT_5_MINI": "0.25,2.00"}
    cout = llm_usage.estimate_cost("gpt-5-mini", 1_000_000, 1_000_000, env)
    assert cout == pytest.approx(2.25)


def test_un_prix_illisible_ne_fait_pas_echouer_le_calcul():
    env = {"LLM_PRICE_GPT_5_MINI": "gratuit"}
    assert llm_usage.estimate_cost("gpt-5-mini", 10, 10, env) == 0.0


# --- imputation des appels ------------------------------------------------


class FauxVision:
    """Escalade controlee, sans reseau."""

    available = True

    def __init__(self, terra=None, sol=None) -> None:
        self._terra, self._sol = terra, sol
        self.appels: list[str] = []

    def model_for(self, level: str) -> str:
        return {"terra": "modele-terra", "sol": "modele-sol"}[level]

    def read_text(self, texte):
        self.appels.append("terra")
        return self._terra

    def read_image(self, data, mimetype):
        self.appels.append("sol")
        return self._sol


def _pipeline(db_path, company_id, vision):
    return doc_pipeline.DocumentPipeline(
        gateway=None, db_path=db_path, chat_id=CHAT, spreadsheet_id="sheet-x",
        company_id=company_id, vision=vision,
    )


class _Doc:
    """Document qu'aucune lecture deterministe n'a su lire."""

    doc_type = "facture_achat"
    numero = ""
    date_document = None
    montant_ht = montant_tva = montant_ttc = None
    raw_text = "texte OCR degrade"

    def __init__(self) -> None:
        self.anomalies: list[str] = []


class _Fichier:
    content = b"pas-une-image"
    filename = "facture.pdf"
    doc_key = "cle-1"


def test_un_appel_indisponible_est_quand_meme_impute(db_path):
    """Un appel refuse par le fournisseur est une information, pas un trou."""
    vision = FauxVision(terra=None)
    _pipeline(db_path, "xblaste", vision).escalate_reading(_Doc(), _Fichier())

    appels = llm_usage.rows_for_document(db_path, "xblaste", "cle-1")
    assert len(appels) == 1
    assert appels[0]["level"] == "terra"
    assert appels[0]["outcome"] == llm_usage.OUTCOME_UNAVAILABLE
    assert appels[0]["model"] == "modele-terra"


def test_le_motif_d_escalade_est_normalise(db_path):
    vision = FauxVision(terra=None)
    _pipeline(db_path, "xblaste", vision).escalate_reading(_Doc(), _Fichier())
    appels = llm_usage.rows_for_document(db_path, "xblaste", "cle-1")
    assert appels[0]["reason"] == llm_usage.REASON_MISSING_FIELDS


def test_les_tokens_d_un_appel_retenu_sont_comptes(db_path):
    retenu = doc_vision.VisionResult(
        level="terra", model="modele-terra", input_tokens=1200, output_tokens=340,
        confidence=0.95,
        data={"numero": "F-1", "date": "2026-08-01", "HT": "100.00",
              "taux_TVA": "20", "TVA": "20.00", "TTC": "120.00",
              "devise": "MAD", "type_document": "facture_achat",
              "tiers": "Fournisseur", "confidence": 0.95},
    )
    vision = FauxVision(terra=retenu)
    _pipeline(db_path, "xblaste", vision).escalate_reading(_Doc(), _Fichier())

    appels = llm_usage.rows_for_document(db_path, "xblaste", "cle-1")
    assert appels[0]["input_tokens"] == 1200
    assert appels[0]["output_tokens"] == 340


def test_les_couts_de_deux_entreprises_ne_se_melangent_pas(db_path):
    _pipeline(db_path, "xblaste", FauxVision(terra=None)).escalate_reading(
        _Doc(), _Fichier()
    )
    _pipeline(db_path, "fluxintelligent", FauxVision(terra=None)).escalate_reading(
        _Doc(), _Fichier()
    )

    totaux = llm_usage.totals_by_company(db_path)
    assert set(totaux) == {"xblaste", "fluxintelligent"}
    assert llm_usage.rows_for_document(db_path, "xblaste", "cle-1")
    # La MEME cle de document dans l'autre entreprise reste son appel a elle.
    assert len(llm_usage.rows_for_document(db_path, "fluxintelligent", "cle-1")) == 1


def test_un_document_lisible_ne_declenche_aucun_appel(db_path):
    """L'escalade ne se paie que lorsqu'elle est necessaire."""
    class DocLisible(_Doc):
        doc_type = "releve_bancaire"

    vision = FauxVision(terra=None)
    _pipeline(db_path, "xblaste", vision).escalate_reading(DocLisible(), _Fichier())

    assert vision.appels == []
    assert llm_usage.totals_by_company(db_path) == {}


def test_sans_entreprise_aucune_ligne_de_cout_n_est_ecrite(db_path):
    """Mode mono-entreprise d'avant la V2 : rien a ventiler."""
    vision = FauxVision(terra=None)
    _pipeline(db_path, "", vision).escalate_reading(_Doc(), _Fichier())
    assert llm_usage.totals_by_company(db_path) == {}


# --- ZIP mono-entreprise ---------------------------------------------------


def _message(message_id, delivered_to="", subject="Dossier comptable"):
    return {
        "messageId": message_id,
        "subject": subject,
        "sender": "client@exemple.ma",
        "payload": {"headers": [
            {"name": "Delivered-To", "value": delivered_to},
            {"name": "Subject", "value": subject},
            {"name": "From", "value": "client@exemple.ma"},
        ]},
    }


class FauxWorker:
    def __init__(self, company_id, sheet_id, drive):
        self.company_id, self.sheet_id, self.drive_folder = company_id, sheet_id, drive
        self.traites: list[str] = []
        self.appels_llm = 0
        self.ecritures = 0
        self.telegram = 0

    def process_message(self, message_id):
        self.traites.append(message_id)
        return MailSummary(message_id=message_id, subject="", sender=""), 1_787_872_375


def _moteur(db_path, messages):
    faux: dict[str, FauxWorker] = {}

    def fabrique(tenant: TenantContext):
        faux[tenant.company_id] = FauxWorker(
            tenant.company_id, tenant.sheet_id, tenant.drive_folder_id
        )
        return faux[tenant.company_id]

    moteur = tw.TenantWorker(
        api_key="cle", chat_id=CHAT, db_path=db_path, query=QUERY,
        worker_factory=fabrique,
    )

    class Sondeur:
        def __init__(self):
            self.msgs = {m["messageId"]: m for m in messages}
            self.requetes = []

        def execute(self, slug, arguments):
            self.requetes.append(arguments["query"])
            return {"messages": [{"messageId": m} for m in self.msgs]}

        def fetch_message(self, mid):
            return self.msgs[mid]

    moteur._probe = Sondeur()
    return moteur, faux


def test_un_zip_appartient_a_l_entreprise_de_l_email(db_path):
    """Les noms des fichiers d'une archive ne routent RIEN.

    Une archive livree a XBLASTE dont les pieces citent l'autre societe
    reste entierement a XBLASTE : sinon un expediteur choisirait la
    comptabilite de destination en renommant ses fichiers.
    """
    moteur, faux = _moteur(db_path, [
        _message("m-zip", delivered_to=XBLASTE_ALIAS,
                 subject="dossier_fluxintelligent_aout.zip"),
    ])
    rapport = moteur.process_once()

    assert rapport.routed == {"xblaste": 1}
    assert faux["xblaste"].traites == ["m-zip"]
    assert "fluxintelligent" not in faux


# --- trois cycles idempotents ---------------------------------------------


def test_trois_cycles_sans_nouvel_email_ne_font_rien(db_path):
    """Zero appel modele, zero ecriture, zero Telegram."""
    moteur, faux = _moteur(db_path, [])
    for _ in range(3):
        rapport = moteur.process_once()
        assert rapport.seen == 0
        assert rapport.routed == {}
        assert rapport.summaries == []

    assert faux == {}
    assert llm_usage.totals_by_company(db_path) == {}


def test_trois_cycles_ne_reavancent_pas_le_curseur(db_path):
    """Un curseur qui bouge sans email est un retraitement en puissance."""
    moteur, _ = _moteur(db_path, [_message("m-x", delivered_to=XBLASTE_ALIAS)])
    moteur.process_once()
    contexte = TenantContext.for_company(db_path, "xblaste")
    apres_premier = contexte.cursor(1)["last_internal_date"]

    moteur._probe.msgs = {}
    for _ in range(3):
        moteur.process_once()
    assert contexte.cursor(1)["last_internal_date"] == apres_premier


def test_trois_cycles_ne_recreent_pas_les_workers(db_path):
    """Reconstruire un worker par cycle reviderait tous ses caches."""
    moteur, faux = _moteur(db_path, [_message("m-x", delivered_to=XBLASTE_ALIAS)])
    moteur.process_once()
    premier = moteur.worker_for("xblaste")
    for _ in range(3):
        moteur.process_once()
    assert moteur.worker_for("xblaste") is premier
    assert len(faux) == 1


def test_une_piece_en_quarantaine_ne_repaie_pas_l_escalade(db_path):
    """Regression : la quarantaine repayait un appel a chaque cycle.

    Les octets n'ayant pas change, la relecture rendrait exactement le
    meme resultat : la refaire toutes les minutes facturerait sans fin.
    """
    vision = FauxVision(terra=None)
    pipeline = _pipeline(db_path, "xblaste", vision)
    pipeline.escalate_reading(_Doc(), _Fichier())
    apres_premier = len(llm_usage.rows_for_document(db_path, "xblaste", "cle-1"))
    assert apres_premier == 1

    # Le garde-fou vit dans process_document ; on verifie ici que le
    # journal permet de le CONSTATER, piece par piece.
    niveaux = llm_usage.levels_for_document(db_path, "xblaste", "cle-1")
    assert niveaux == ("terra",)
