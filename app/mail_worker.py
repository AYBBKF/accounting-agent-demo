"""Worker Gmail multi-documents.

Remplace l'ancien worker mono-facture. Pour chaque email posterieur au
curseur :

  1. toutes les pieces jointes sont recuperees, ZIP compris ;
  2. chaque document est traite INDEPENDAMMENT par le pipeline - une erreur
     sur l'un n'empeche jamais les autres d'aboutir ;
  3. un resume compact est envoye dans Telegram, avec des boutons pour les
     seuls documents reellement ambigus.

Gmail reste en LECTURE : la liste des outils appeles ne contient aucun
envoi, aucune suppression, aucune modification. Aucun secret n'est
journalise ni renvoye dans un message.
"""
from __future__ import annotations

import re
import time

import hashlib
from decimal import Decimal
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app import doc_store as store
from app import doc_vision
from app import doc_vault as vault
from app.attachments import (
    DocumentFile,
    ZipLimits,
    collect_documents,
    extract_member,
    idempotency_key,
    is_zip,
    sha256_of,
)
from app.review_sheet import TAB_REVIEW
from app.doc_pipeline import DocumentOutcome, DocumentPipeline
from app.doc_policy import ACTION_AUTO, ACTION_DUPLICATE, ACTION_REVIEW, ACTION_UNKNOWN
from app.doc_types import LABELS

logger = logging.getLogger("demo_bot.mail_worker")

# Saut de ligne sans sequence d'echappement : le transport JSON des outils
# de publication reinterprete les sequences d'echappement et corromprait le
# fichier. Un module sans antislash traverse la chaine intact.
NL = chr(10)

COMPOSIO_BASE_URL = "https://backend.composio.dev"
COMPOSIO_TOOLS_EXECUTE_PATH = "/api/v3.1/tools/execute/{tool_slug}"
COMPOSIO_FILE_UPLOAD_PATH = "/api/v3.1/files/upload/request"
_REQUEST_TIMEOUT_SECONDS = 60.0
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Telegram limite `callback_data` a 64 octets : le prefixe court de la cle
# d'idempotence suffit a retrouver le document sans risque de collision.


# Etats de NOTIFICATION. Ils ne remplacent pas l'etat metier stocke dans
# SQLite : ils disent ce qui a deja ete ANNONCE au client. Une notification
# n'est envoyee que lors d'une transition reelle entre deux de ces valeurs.
NOTIFY_NEW = ""
NOTIFY_COMPLETED = "completed"
NOTIFY_WAITING = "waiting_validation"
NOTIFY_SKIPPED = "skipped"

# Nombre maximal de lignes de detail dans un resume Telegram. Au-dela,
# on annonce le compte : le detail exhaustif vit dans 14_IMPORTS_LOG et
# dans les journaux serveur, pas dans un message de conversation.
MAX_DETAIL_LINES = 10
NOTIFY_PARTIAL = "partial"
NOTIFY_FAILED = "failed"
NOTIFY_REJECTED = "rejected"

# Motifs de rejet qui ne meritent AUCUN message : un ZIP comptable contient
# normalement un manifeste, un README ou une somme de controle. Ce ne sont
# pas des documents, leur presence n'est pas une anomalie.
SILENT_REJECTIONS = (
    # Membre de ZIP qui n'est ni PDF ni image (README, manifeste, CSV, somme
    # de controle). Les deux libelles - avant et apres l'ajout des images -
    # restent reconnus pour ne rien annoncer.
    "n'est ni un PDF ni une image (signature invalide)",
    "n'est pas un PDF (signature invalide)",
    # Piece jointe directe qui n'est ni document ni archive.
    "piece jointe ignoree : ni PDF, ni image, ni archive ZIP",
    "piece jointe ignoree : ni PDF ni archive ZIP",
)


# L'etat METIER stocke en base decide de l'etat NOTIFIABLE. C'est lui qui
# fait foi : deux cycles successifs qui laissent un document dans le meme
# etat metier doivent produire exactement le meme etat notifiable, donc
# aucun message.
_STATE_TO_NOTIFY = {
    store.COMPLETED: NOTIFY_COMPLETED,
    store.DUPLICATE: NOTIFY_COMPLETED,
    store.SKIPPED: NOTIFY_SKIPPED,
    store.NEEDS_REVIEW: NOTIFY_WAITING,
    store.PARTIAL: NOTIFY_PARTIAL,
    store.FAILED: NOTIFY_FAILED,
}


def notify_state_of(outcome: "DocumentOutcome", state: str = "") -> str:
    """Etat qu'il faudrait annoncer pour ce resultat.

    ACTION_DUPLICATE ne produit jamais d'etat propre : un document deja
    importe reste 'completed', donc identique au dernier etat notifie, donc
    silencieux. C'est exactement la regle demandee.
    """
    connu = _STATE_TO_NOTIFY.get(str(state or ""))
    if connu:
        return connu
    if outcome.error:
        return NOTIFY_FAILED
    if outcome.action == ACTION_REVIEW:
        return NOTIFY_WAITING
    return NOTIFY_COMPLETED


def is_silent_rejection(reason: str) -> bool:
    return any(motif in reason for motif in SILENT_REJECTIONS)


# Bornes de date que l'exploitant peut poser lui-meme dans la requete
# configuree. Si l'une d'elles est presente, le curseur n'en ajoute pas une
# seconde : deux `after:` dans la meme requete rendent un resultat VIDE.
_HAS_DATE_BOUND = re.compile(r"\b(?:after|before|newer_than|older_than):", re.I)


class MailWorkerError(RuntimeError):
    """Erreur destinee aux logs / au client, jamais porteuse de secret."""


# Reprise des appels de LECTURE uniquement (voir MailWorker.execute).
_READ_RETRY_ATTEMPTS = 3
_READ_RETRY_BACKOFF_SECONDS = 2.0

# Un outil est retentable s'il ne modifie RIEN. La liste est explicite :
# deduire "lecture" d'un nom serait un pari, et se tromper sur une ecriture
# creerait des doublons comptables.
_RETRYABLE_READ_SLUGS = frozenset({
    "GOOGLESHEETS_BATCH_GET",
    "GOOGLESHEETS_GET_SPREADSHEET_INFO",
    "GOOGLESHEETS_GET_SHEET_NAMES",
    "GOOGLEDRIVE_FIND_FOLDER",
    "GMAIL_FETCH_EMAILS",
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
    "GMAIL_GET_ATTACHMENT",
    "GOOGLECALENDAR_FIND_EVENT",
})


def is_retryable_read(slug: str) -> bool:
    """Vrai si l'outil est une lecture sans effet de bord."""
    return slug in _RETRYABLE_READ_SLUGS


# Lectures Google Sheets. Leur quota est un compteur GLISSANT PAR MINUTE qui
# se libere tout seul : contrairement au quota Gmail, attendre le fait
# disparaitre. Un lot de treize pieces enchaine plusieurs centaines de
# lectures et touche ce plafond en fin de traitement ; sans attente, le
# rapprochement bancaire s'interrompait au milieu et le journal
# 08_RAPPROCHEMENT restait vide alors que les rapprochements etaient justes.
_SHEETS_READ_SLUGS = frozenset({
    "GOOGLESHEETS_BATCH_GET",
    "GOOGLESHEETS_GET_SPREADSHEET_INFO",
    "GOOGLESHEETS_GET_SHEET_NAMES",
})
_SHEETS_QUOTA_ATTEMPTS = 4
_SHEETS_QUOTA_PAUSE_SECONDS = 20.0


def is_sheets_read(slug: str) -> bool:
    """Vrai si l'outil est une LECTURE Sheets, dont le quota se libere seul."""
    return slug in _SHEETS_READ_SLUGS


class RateLimited(MailWorkerError):
    """Quota de l'API atteint : reessayer tout de suite est contre-productif."""


def looks_rate_limited(reason: str) -> bool:
    """L'API annonce-t-elle un quota atteint ?

    Le fournisseur repond "HTTP 429: User-rate limit exceeded. Retry after
    <date>". Chaque nouvel appel repousse cette date : insister pendant la
    fenetre de blocage EMPECHE le quota de se liberer. On sort donc
    immediatement, et le cycle suivant retentera plus tard.
    """
    bas = (reason or "").lower()
    return "429" in bas or "rate limit" in bas or "quota" in bas


@dataclass
class MailSummary:
    """Bilan d'UN email, tel qu'il sera resume dans Telegram."""

    message_id: str
    subject: str
    sender: str
    outcomes: list[DocumentOutcome] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    # Sous-ensemble de `outcomes` correspondant a une TRANSITION reelle
    # d'etat. Seul ce sous-ensemble est annonce dans Telegram ; `outcomes`
    # reste complet pour les journaux et les comptages internes.
    notifiable: list[DocumentOutcome] = field(default_factory=list)
    silenced: int = 0
    # Rejets reellement dignes d'un message (les membres non-PDF d'un ZIP
    # sont ecartes en amont, silencieusement).
    notifiable_rejected: list[tuple[str, str]] = field(default_factory=list)
    # Documents perdus par une limite de depaquetage. Toujours annonce,
    # jamais silencieux : c'est une perte, pas un filtrage.
    truncated: int = 0
    planned: bool = False

    def count(self, action: str) -> int:
        return sum(1 for o in self.notified_outcomes if o.action == action)

    @property
    def notified_outcomes(self) -> list[DocumentOutcome]:
        """Ce que le client doit voir : rien si aucun etat n'a change."""
        return self.notifiable if self.planned else self.outcomes

    @property
    def notified_rejected(self) -> list[tuple[str, str]]:
        return self.notifiable_rejected if self.planned else self.rejected

    @property
    def should_notify(self) -> bool:
        return bool(self.notified_outcomes or self.notified_rejected)

    @property
    def imported(self) -> list[DocumentOutcome]:
        return [o for o in self.notified_outcomes if o.action == ACTION_AUTO and o.accounting]

    @property
    def classified(self) -> list[DocumentOutcome]:
        return [
            o for o in self.notified_outcomes
            if o.action == ACTION_AUTO and not o.accounting
        ]

    @property
    def to_review(self) -> list[DocumentOutcome]:
        return [o for o in self.notified_outcomes if o.action == ACTION_REVIEW]

    @property
    def errors(self) -> list[DocumentOutcome]:
        return [
            o for o in self.notified_outcomes
            if o.error and o.action != ACTION_AUTO
        ]


class MailWorker:
    """Interroge Gmail et confie chaque document au pipeline."""

    def __init__(
        self,
        api_key: str,
        chat_id: int,
        db_path: str,
        spreadsheet_id: str = "",
        query: str = "in:inbox has:attachment",
        poll_seconds: int = 60,
        company_name: str = "X BLASTE",
        drive_folder: str = "XBLASTE - Factures",
        max_per_cycle: int = 5,
        zip_limits: ZipLimits | None = None,
        calendar_check: str = "",
        allowed_vat_rates: tuple[Decimal, ...] | None = None,
        vision: Any | None = None,
        vision_max_calls: int = 0,
    ) -> None:
        self._vision = vision
        # Budget d'appels au niveau vision, remis a zero a CHAQUE email.
        self._vision_budget = doc_vision.VisionBudget(vision_max_calls)
        self._api_key = api_key
        self._chat_id = chat_id
        self._db_path = db_path
        self._spreadsheet_id = spreadsheet_id
        self._query = query
        self._poll_seconds = poll_seconds
        self._company = company_name
        self._drive_folder = drive_folder
        self._max_per_cycle = max_per_cycle
        self._zip_limits = zip_limits or ZipLimits()
        self._vat_rates = tuple(allowed_vat_rates) if allowed_vat_rates else ()
        self._client = None
        self._pipeline: DocumentPipeline | None = None
        self._startup_done = False
        # Evenement d'echeance a relire au demarrage, en lecture seule.
        self._calendar_check = calendar_check or os.environ.get(
            "CALENDAR_CHECK_EVENT_ID", ""
        )

    # -- proprietes --------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key) and bool(self._chat_id)

    @property
    def poll_seconds(self) -> int:
        return self._poll_seconds

    @property
    def query(self) -> str:
        return self._query

    @property
    def user_id(self) -> str:
        from app.composio_connect import composio_user_id_for_chat

        return composio_user_id_for_chat(self._chat_id)

    @property
    def pipeline(self) -> DocumentPipeline:
        if self._pipeline is None:
            store.ensure_schema(self._db_path)
            self._pipeline = DocumentPipeline(
                self, db_path=self._db_path, chat_id=self._chat_id,
                spreadsheet_id=self._spreadsheet_id, company=self._company,
                drive_root=self._drive_folder,
                allowed_vat_rates=self._vat_rates or None,
                vision=self._vision, vision_budget=self._vision_budget,
            )
        return self._pipeline

    # -- transport ---------------------------------------------------------

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise MailWorkerError("COMPOSIO_API_KEY manquant.")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise MailWorkerError(f"Dependance httpx manquante: {exc}") from exc
        try:
            self._client = httpx.Client(
                base_url=COMPOSIO_BASE_URL,
                headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - l'en-tete porte la cle API
            logger.warning("Client HTTP non initialisable: %s", type(exc).__name__)
            raise MailWorkerError("Client HTTP Composio non initialisable.") from exc
        return self._client

    def execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Appel Composio, isole par `user_id = telegram_<chat_id>`.

        Les appels de LECTURE sont retentes : un lot de documents enchaine
        des centaines de lectures Sheets en quelques secondes et l'API
        repond alors ponctuellement en erreur (quota par minute). Sans
        reprise, un simple hoquet de lecture faisait echouer le document
        entier ("GOOGLESHEETS_BATCH_GET a echoue") alors que rien n'etait
        anormal dans la piece. Les ECRITURES ne sont jamais retentees : une
        ecriture donnee pour perdue peut avoir abouti, et la rejouer
        creerait une seconde ligne comptable.
        """
        attempts = _READ_RETRY_ATTEMPTS if is_retryable_read(slug) else 1
        last: Exception | None = None
        quota_wait = 0
        attempt = 0
        while attempt < attempts:
            attempt += 1
            try:
                return self._execute_once(slug, arguments)
            except RateLimited as exc:
                # Quota Gmail : chaque appel repousse la fenetre, on sort.
                # Quota de LECTURE Sheets : le compteur est par minute et se
                # libere seul, alors on patiente au lieu d'abandonner un
                # rapprochement a moitie fait. Aucune ecriture n'est jamais
                # concernee : rejouer une ecriture creerait une double
                # ecriture comptable.
                if not is_sheets_read(slug) or quota_wait >= _SHEETS_QUOTA_ATTEMPTS:
                    raise
                quota_wait += 1
                logger.info(
                    "Quota de lecture Sheets atteint sur '%s' "
                    "(attente %d/%d, %.0fs)",
                    slug, quota_wait, _SHEETS_QUOTA_ATTEMPTS,
                    _SHEETS_QUOTA_PAUSE_SECONDS,
                )
                time.sleep(_SHEETS_QUOTA_PAUSE_SECONDS)
                last = exc
                # L'attente d'un quota n'est pas un essai rate : elle ne
                # doit pas consommer les tentatives de relecture.
                attempt -= 1
                continue
            except MailWorkerError as exc:
                last = exc
                if attempt >= attempts:
                    break
                pause = _READ_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.info(
                    "Lecture '%s' en echec (essai %d/%d), nouvelle tentative dans %.1fs",
                    slug, attempt, attempts, pause,
                )
                time.sleep(pause)
        assert last is not None
        raise last

    def _execute_once(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        path = COMPOSIO_TOOLS_EXECUTE_PATH.format(tool_slug=slug)
        try:
            response = client.post(path, json={"arguments": arguments, "user_id": self.user_id})
            response.raise_for_status()
            result = response.json()
        except Exception as exc:  # noqa: BLE001 - jamais de secret dans le message
            logger.warning("Outil '%s' injoignable (user=%s): %s", slug, self.user_id, exc)
            raise MailWorkerError(f"Appel '{slug}' impossible.") from exc
        if not result.get("successful", False):
            # Le motif rendu par l'API est journalise : sans lui, un quota
            # depasse et une erreur de droits etaient indiscernables.
            reason = str(result.get("error") or "")
            logger.warning(
                "Outil '%s' en echec (user=%s): %s", slug, self.user_id, reason[:200],
            )
            if looks_rate_limited(reason):
                raise RateLimited(f"Quota atteint sur '{slug}'.")
            raise MailWorkerError(f"L'outil '{slug}' a echoue.")
        return result.get("data") or {}

    def upload(self, *, name: str, mimetype: str, content: bytes) -> str:
        """Depose les octets reels du document chez Composio et retourne la
        cle S3 a passer a GOOGLEDRIVE_UPLOAD_FILE.

        Indispensable pour un PDF membre d'un ZIP : son contenu n'a aucune
        URL Gmail propre, et archiver l'URL du ZIP parent reviendrait a
        stocker l'archive entiere sous le nom du PDF.
        """
        import hashlib

        client = self._ensure_client()
        empreinte = hashlib.md5(content).hexdigest()
        try:
            response = client.post(
                COMPOSIO_FILE_UPLOAD_PATH,
                json={
                    "toolkit_slug": "googledrive",
                    "tool_slug": "GOOGLEDRIVE_UPLOAD_FILE",
                    "filename": name,
                    "mimetype": mimetype,
                    "md5": empreinte,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - jamais de secret dans le message
            logger.warning("Preparation du depot de %s impossible: %s", name, exc)
            raise MailWorkerError("Depot du document impossible.") from exc

        key = str(payload.get("key") or "")
        presigned = str(payload.get("new_presigned_url") or "")
        if not key:
            raise MailWorkerError("Depot du document refuse (aucune cle).")
        if presigned:
            try:
                import httpx

                put = httpx.put(
                    presigned, content=content,
                    headers={"Content-Type": mimetype},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
                put.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Envoi du contenu de %s echoue: %s", name, exc)
                raise MailWorkerError("Envoi du document impossible.") from exc
        return key

    # -- curseur -----------------------------------------------------------

    def cursor(self) -> dict[str, Any]:
        store.ensure_schema(self._db_path)
        return store.get_or_init_cursor(
            self._db_path, self._chat_id, int(datetime.now(timezone.utc).timestamp())
        )

    def effective_query(self) -> str:
        """Requete Gmail reellement envoyee, curseur compris.

        Deux pieges reels, verifies contre l'API :

          - Gmail ne rend AUCUN message pour `after:0`. La borne est
            rejetee, pas interpretee comme "depuis toujours".
          - deux bornes `after:` dans la meme requete ne se combinent pas :
            l'ensemble resultat est VIDE. Une borne posee par
            l'exploitant dans la requete configuree etait donc annulee par
            celle du curseur, et le worker ne trouvait plus jamais rien -
            en silence, sans erreur.

        Quand la requete configuree porte deja sa propre borne de date,
        c'est elle qui fait foi et le curseur n'en ajoute pas une seconde.
        C'est aussi le seul moyen de faire relire une periode passee dans
        un environnement neuf, dont le curseur demarre a l'instant present.
        La deduplication reste seule garante contre la double ecriture.
        """
        if _HAS_DATE_BOUND.search(self._query):
            return self._query
        floor = store.query_floor(self.cursor())
        if floor <= 0:
            return self._query
        return f"{self._query} after:{floor}"

    def rewind(self, hours: int = 24) -> int:
        """Recul volontaire du curseur (/reprocess). Les documents deja
        traites restent proteges par leur cle d'idempotence."""
        store.ensure_schema(self._db_path)
        return store.rewind_cursor(self._db_path, self._chat_id, hours * 3600)

    # -- lecture Gmail -----------------------------------------------------

    def search_messages(self) -> list[dict[str, Any]]:
        data = self.execute(
            "GMAIL_FETCH_EMAILS",
            {"query": self.effective_query(), "max_results": self._max_per_cycle},
        )
        return data.get("messages") or []

    def fetch_message(self, message_id: str) -> dict[str, Any]:
        return self.execute(
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": message_id, "format": "full"}
        )

    # Extensions et types MIME des pieces jointes exploitables. Les images
    # (facture photographiee) sont selectionnees comme les PDF : la decision
    # finale PDF/image/ZIP se prend en aval sur la SIGNATURE binaire, mais le
    # premier tri, cote Gmail, se fait sur le nom et le type annonce.
    _KEEP_EXTENSIONS = (".pdf", ".zip", ".png", ".jpg", ".jpeg")
    _KEEP_MIMES = frozenset({
        "application/pdf",
        "application/zip",
        "application/x-zip-compressed",
        "image/png",
        "image/jpeg",
        "image/jpg",
    })

    @staticmethod
    def attachments_of(message: dict[str, Any]) -> list[dict[str, Any]]:
        """Pieces jointes exploitables : PDF, archives ZIP et images (PNG/JPEG)."""
        keep = []
        for att in message.get("attachmentList") or []:
            name = str(att.get("filename") or "").lower()
            mime = str(att.get("mimeType") or "").lower()
            if name.endswith(MailWorker._KEEP_EXTENSIONS) or mime in MailWorker._KEEP_MIMES:
                keep.append(att)
        return keep

    def download(self, message_id: str, attachment: dict[str, Any]) -> tuple[bytes, str]:
        """Contenu binaire + URL source (reutilisee pour l'archivage Drive)."""
        data = self.execute(
            "GMAIL_GET_ATTACHMENT",
            {
                "message_id": message_id,
                "attachment_id": attachment.get("attachmentId"),
                "file_name": attachment.get("filename"),
            },
        )
        file_info = data.get("file")
        if isinstance(file_info, str):
            import ast

            try:
                file_info = ast.literal_eval(file_info)
            except (ValueError, SyntaxError) as exc:
                raise MailWorkerError("Reponse de piece jointe illisible.") from exc
        if not isinstance(file_info, dict):
            raise MailWorkerError("Piece jointe absente de la reponse Gmail.")
        url = file_info.get("s3url") or file_info.get("url")
        if not url:
            raise MailWorkerError("Aucune URL de telechargement pour la piece jointe.")
        try:
            import httpx

            response = httpx.get(url, timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
            response.raise_for_status()
            content = response.content
        except Exception as exc:  # noqa: BLE001 - l'URL signee n'est jamais journalisee
            raise MailWorkerError("Telechargement de la piece jointe impossible.") from exc
        if len(content) > _MAX_ATTACHMENT_BYTES:
            raise MailWorkerError(f"Piece jointe trop volumineuse ({len(content)} octets).")
        return content, str(url)

    # -- cycle -------------------------------------------------------------

    def process_once(self) -> list[MailSummary]:
        """Un tour de boucle : reprises d'abord, puis nouveaux emails."""
        # Le schema doit exister AVANT la premiere lecture : au tout premier
        # demarrage sur un volume neuf, la reprise s'executait avant que le
        # pipeline (qui cree les tables paresseusement) n'ait ete construit,
        # et le worker mourait sur "no such table: documents".
        store.ensure_schema(self._db_path)
        self.run_startup_tasks()
        summaries: list[MailSummary] = []
        resumed = self.finish_unfinished()
        if resumed:
            reprise = MailSummary(
                message_id="(reprises)", subject="Reprise d'imports interrompus",
                sender="", outcomes=resumed,
            )
            self.plan_notifications(reprise)
            if reprise.should_notify:
                summaries.append(reprise)
        messages = self.search_messages()
        logger.info(
            "Cycle Gmail (user=%s): %d email(s) correspondant a la requete",
            self.user_id, len(messages),
        )
        newest = 0
        for meta in messages:
            message_id = meta.get("messageId") or meta.get("id")
            if not message_id:
                continue
            try:
                summary, internal_date = self.process_message(str(message_id))
            except (MailWorkerError, Exception) as exc:  # noqa: BLE001
                logger.warning("Email %s ignore: %s", message_id, exc)
                continue
            newest = max(newest, internal_date)
            if summary.should_notify:
                summaries.append(summary)
            else:
                logger.info(
                    "Email %s : aucun changement d'etat, 0 message Telegram.",
                    message_id,
                )
        if newest:
            store.advance_cursor(self._db_path, self._chat_id, newest)
        return summaries

    def run_startup_tasks(self) -> None:
        """Taches ponctuelles au demarrage, protegees par un marqueur.

        L'API de l'hebergeur ne permet aucun `docker exec` : une correction
        de donnees doit donc voyager avec l'image et se declencher seule, une
        seule fois, dans le processus de production. Un echec ici ne doit
        jamais empecher le cycle Gmail de tourner.
        """
        if self._startup_done:
            return
        self._startup_done = True
        try:
            from app import drive_repair

            report = drive_repair.run(self)
        except Exception as exc:  # noqa: BLE001 - jamais bloquant
            logger.exception("Tache de demarrage en echec: %s", type(exc).__name__)
            return
        if report.get("skipped"):
            logger.info("Migration des archives : %s", report.get("reason"))
        else:
            for entry in report.get("repaired", []):
                logger.info(
                    "Archive reparee %s : %s -> %s (%s, %s octets, %s)",
                    entry["fichier"], entry["ancien_id"], entry["nouveau_id"],
                    entry["dossier"], entry["taille"], entry["controle"],
                )
            for entry in report.get("failed", []):
                logger.warning(
                    "Archive non reparee %s : %s", entry["doc_key"], entry["erreur"]
                )
            for entry in report.get("quarantined", []):
                logger.info("Ancien fichier %s : %s", entry["id"], entry["etat"])
        self.check_calendar_event()

    def check_calendar_event(self) -> None:
        """Verification EN LECTURE SEULE de l'evenement d'echeance.

        Le classeur porte un identifiant d'evenement ; un identifiant stocke
        ne prouve pas qu'un evenement existe. On relit donc l'evenement avec
        la connexion de production, sans rien creer ni modifier.
        """
        event_id = str(self._calendar_check or "").strip()
        if not event_id:
            return
        try:
            from app import drive_repair

            found = drive_repair.read_calendar_event(self, event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Controle Calendar impossible: %s", type(exc).__name__)
            return
        if found.get("found"):
            logger.info(
                "Calendar (lecture seule) %s : titre=%r debut=%s fuseau=%s "
                "statut=%s agenda=%s",
                found["id"], found["summary"], found["start"], found["timezone"],
                found["status"], found["calendar"],
            )
        else:
            logger.warning(
                "Calendar (lecture seule) %s : evenement introuvable (%s)",
                event_id, found.get("error", ""),
            )

    def process_message(self, message_id: str) -> tuple[MailSummary, int]:
        """Traite toutes les pieces jointes d'un email, independamment."""
        # Le budget de relecture visuelle est propre a CHAQUE email.
        self._vision_budget.reset()
        message = self.fetch_message(message_id)
        message.setdefault("messageId", message_id)
        summary = MailSummary(
            message_id=message_id,
            subject=str(message.get("subject") or ""),
            sender=str(message.get("sender") or ""),
        )
        internal_date = _internal_date(message)

        for attachment in self.attachments_of(message):
            attachment_id = str(attachment.get("attachmentId") or attachment.get("filename") or "")
            name = str(attachment.get("filename") or "piece-jointe")
            try:
                content, source_url = self.download(message_id, attachment)
            except MailWorkerError as exc:
                summary.rejected.append((name, str(exc)))
                continue
            report = collect_documents(name, content, limits=self._zip_limits)
            summary.rejected.extend(report.rejected)
            if report.truncated:
                # Une archive tronquee est une PERTE de documents, pas un
                # rejet ordinaire. Elle etait auparavant diluee en une ligne
                # par fichier ecarte, au milieu d'un resume deja trop long :
                # personne ne la voyait. Elle a desormais son propre message,
                # son propre niveau de journal, et un compte exact.
                summary.truncated += report.truncated
                logger.error(
                    "Archive %s tronquee : %d document(s) NON traites "
                    "(limite de %d fichiers). Aucun de ces documents n'a ete lu.",
                    name, report.truncated, self._zip_limits.max_files,
                )
            for file in report.files:
                try:
                    summary.outcomes.append(
                        self.process_file(
                            file, message, message_id,
                            attachment_id=attachment_id, parent_filename=name,
                            source_url=source_url,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - un document ne bloque pas les autres
                    logger.exception("Document %s en echec", file.display_name)
                    summary.outcomes.append(
                        DocumentOutcome(
                            doc_key=idempotency_key(
                                self.user_id, message_id, file.stable_ref, file.sha256
                            ),
                            filename=file.display_name,
                            action=ACTION_REVIEW,
                            error=str(exc),
                            reasons=[f"erreur technique : {exc}"],
                        )
                    )
        self.plan_notifications(summary)
        return summary, internal_date

    # -- idempotence des notifications -------------------------------------

    def plan_notifications(self, summary: MailSummary) -> MailSummary:
        """Ne retient que les TRANSITIONS reelles d'etat.

        Le bug corrige ici etait entier a cet endroit : le worker renvoyait
        au client tout ce qu'il venait de recalculer, sans jamais se demander
        si quelque chose avait change depuis le cycle precedent. Un document
        deja termine restait 'deja importe' a chaque tour, un document en
        attente redemandait sa validation toutes les soixante secondes.
        """
        retenus: list[DocumentOutcome] = []
        silencieux = 0
        for outcome in summary.outcomes:
            fiche = store.get_document(self._db_path, outcome.doc_key) or {}
            if str(fiche.get("superseded_by") or ""):
                # Fichier deja connu, rattache a sa fiche canonique : rien
                # n'a ete ecrit, rien n'a change. L'annoncer document par
                # document reviendrait a envoyer trente-huit messages pour
                # dire trente-huit fois "rien n'a bouge". Le compte figure
                # dans le resume et dans les journaux ; c'est suffisant.
                silencieux += 1
                continue
            etat = notify_state_of(outcome, str(fiche.get("state") or ""))
            deja = str(fiche.get("last_notified_state") or "")
            if etat == deja:
                silencieux += 1
                continue
            retenus.append(outcome)
        summary.notifiable = retenus
        summary.silenced = silencieux

        # Un .txt, un .csv, un README ou une somme de controle dans un ZIP
        # comptable n'est pas une anomalie : il est ecarte SILENCIEUSEMENT,
        # sans notification, sans validation et sans trace dans le journal
        # comptable. `summary.rejected` garde la verite technique pour les
        # journaux serveur ; seul l'affichage client est filtre.
        rejets = [
            (nom, motif) for nom, motif in summary.rejected
            if not is_silent_rejection(motif)
        ]
        if rejets:
            signature = hashlib.sha256(
                repr(sorted(rejets)).encode("utf-8")
            ).hexdigest()
            connue = store.email_notification_signature(
                self._db_path, self._chat_id, summary.message_id
            )
            if signature == connue:
                rejets = []
            else:
                store.remember_email_notification(
                    self._db_path, self._chat_id, summary.message_id, signature
                )
        summary.notifiable_rejected = rejets
        summary.planned = True
        if silencieux:
            logger.info(
                "Email %s : %d document(s) sans changement d'etat, aucune "
                "notification envoyee pour eux.",
                summary.message_id, silencieux,
            )
        return summary

    def mark_notified(
        self, outcome: DocumentOutcome, *, telegram_message_id: int = 0
    ) -> None:
        """A appeler APRES un envoi Telegram reussi, jamais avant.

        Si l'envoi echoue, l'etat notifie reste inchange et le message sera
        retente au cycle suivant : c'est la seule facon de ne perdre aucune
        demande de validation sans en renvoyer aucune en trop.
        """
        fiche = store.get_document(self._db_path, outcome.doc_key) or {}
        store.mark_notified(
            self._db_path, outcome.doc_key,
            notify_state_of(outcome, str(fiche.get("state") or "")),
            telegram_message_id=telegram_message_id,
        )

    def pending_validations(self) -> list[DocumentOutcome]:
        """Documents encore en attente de decision, sans rien reecrire.

        Sert a la commande manuelle /resend_pending. Aucune ecriture Sheets,
        Drive ou Calendar n'est declenchee : on relit la fiche stockee et on
        reconstruit le message a partir du PDF deja conserve dans le coffre.
        """
        store.ensure_schema(self._db_path)
        outcomes: list[DocumentOutcome] = []
        for row in store.list_pending_review(self._db_path, self._chat_id):
            try:
                outcome = self.resume(row)
            except Exception as exc:  # noqa: BLE001 - un document n'en bloque pas un autre
                logger.warning(
                    "Renvoi impossible (%s): %s", row["doc_key"][:12], type(exc).__name__
                )
                continue
            if outcome.action == ACTION_REVIEW and not outcome.error:
                outcomes.append(outcome)
        return outcomes

    def process_file(
        self,
        file: DocumentFile,
        message: dict[str, Any],
        message_id: str,
        *,
        attachment_id: str,
        parent_filename: str,
        source_url: str = "",
    ) -> DocumentOutcome:
        """Traite UN document, apres l'avoir mis a l'abri dans le coffre.

        La copie locale est faite AVANT le traitement : si le client valide
        deux heures plus tard, ou apres un redemarrage du conteneur, le PDF
        est deja la et il n'y a plus rien a redecompresser.
        """
        doc_key = idempotency_key(self.user_id, message_id, file.stable_ref, file.sha256)
        known = store.find_by_message_and_sha(
            self._db_path, self._chat_id, message_id, file.sha256
        )
        if known is not None:
            doc_key = known["doc_key"]
        local_path = vault.save(self._db_path, self._chat_id, doc_key, file.content)
        return self.pipeline.process_document(
            file, message, attachment_id=attachment_id, source_url=source_url,
            parent_attachment_id=attachment_id,
            parent_filename=parent_filename, local_path=local_path,
        )

    # -- rehydratation d'un document deja connu ----------------------------

    def materialize(self, row: dict[str, Any]) -> tuple[DocumentFile, str]:
        """Retrouve le PDF d'un document deja enregistre, et son URL source.

        Ordre imperatif, du moins couteux au plus couteux :
          1. le coffre local, sous controle d'empreinte ;
          2. a defaut, l'archive PARENTE retelechargee depuis Gmail, dont on
             extrait le seul `member_path` concerne.

        On ne cherche JAMAIS le PDF enfant comme une piece jointe Gmail
        autonome : il n'en est pas une, et c'est exactement ce qui produisait
        "Piece jointe introuvable dans l'email d'origine".
        """
        expected = str(row["file_sha256"])
        member_path = str(row.get("member_path") or "")
        container = str(row.get("container") or "")
        filename = str(row.get("filename") or "document.pdf")

        content = vault.load(self._db_path, self._chat_id, row["doc_key"], expected)
        if content is not None:
            file = DocumentFile(
                filename=filename, content=content,
                source="zip" if member_path else "attachment",
                container=container, member_path=member_path,
            )
            return file, str(row.get("source_url") or "")

        parent, source_url = self.download_parent(row)
        if member_path:
            extracted = extract_member(parent, member_path)
            if extracted is None:
                raise MailWorkerError(
                    f"'{member_path}' est absent de l'archive '{row.get('parent_filename') or ''}'."
                )
        elif is_zip(parent):
            # Enregistrement anterieur a la persistance du chemin interne :
            # le document vient bien d'une archive, mais on ne sait pas
            # encore d'ou. On le retrouve par son EMPREINTE, seule donnee
            # fiable dont on dispose, puis on complete la fiche pour que la
            # prochaine reprise soit directe.
            extracted, member_path = self._recover_member(parent, expected)
            if extracted is None:
                raise MailWorkerError(
                    "Ce document est introuvable dans l'archive d'origine "
                    "(son contenu a change ou l'archive n'est plus la meme)."
                )
            store.update_document(self._db_path, row["doc_key"], member_path=member_path)
            logger.info(
                "Chemin interne retrouve pour %s : %s", row["doc_key"][:12], member_path
            )
        else:
            extracted = parent

        actual = sha256_of(extracted)
        if actual != expected:
            # Le contenu a change depuis l'analyse : ecrire une ecriture
            # comptable a partir d'un fichier different de celui qui a ete
            # controle serait une faute. On refuse, sans rien ecrire.
            raise MailWorkerError(
                "Le fichier retrouve ne correspond plus au document analyse "
                "(empreinte differente). Aucune ecriture n'a ete faite."
            )
        vault.save(self._db_path, self._chat_id, row["doc_key"], extracted)
        file = DocumentFile(
            filename=filename, content=extracted,
            source="zip" if member_path else "attachment",
            container=container, member_path=member_path,
        )
        return file, source_url

    def _recover_member(
        self, archive: bytes, expected_sha256: str
    ) -> tuple[bytes | None, str]:
        """Retrouve dans une archive le membre portant l'empreinte attendue.

        Sert uniquement a rattraper les documents enregistres avant que le
        chemin interne ne soit persiste. La correspondance se fait sur le
        SHA-256 : aucune supposition sur le nom.
        """
        report = collect_documents("archive.zip", archive, limits=self._zip_limits)
        for candidate in report.files:
            if candidate.sha256 == expected_sha256:
                return candidate.content, candidate.member_path
        return None, ""

    def download_parent(self, row: dict[str, Any]) -> tuple[bytes, str]:
        """Retelecharge la piece jointe PARENTE (le ZIP, ou le PDF lui-meme).

        La piece est retrouvee par son NOM, pas par son `attachmentId` :
        Gmail renvoie un identifiant different a chaque lecture du message,
        et s'y fier rendait toute reprise impossible.
        """
        message_id = str(row["gmail_message_id"])
        message = self.fetch_message(message_id)
        attachments = self.attachments_of(message)
        if not attachments:
            raise MailWorkerError("L'email d'origine ne porte plus de piece jointe.")
        wanted_name = str(row.get("parent_filename") or row.get("container") or "").strip()
        wanted_id = str(row.get("parent_attachment_id") or row.get("attachment_id") or "")
        chosen = (
            next((a for a in attachments if str(a.get("attachmentId")) == wanted_id), None)
            or next(
                (a for a in attachments
                 if str(a.get("filename") or "").strip() == wanted_name), None
            )
            or (attachments[0] if len(attachments) == 1 else None)
        )
        if chosen is None:
            raise MailWorkerError(
                f"Archive '{wanted_name}' introuvable dans l'email d'origine."
            )
        content, source_url = self.download(message_id, chosen)
        store.update_document(
            self._db_path, row["doc_key"],
            attachment_id=str(chosen.get("attachmentId") or ""),
            parent_attachment_id=str(chosen.get("attachmentId") or ""),
            parent_filename=str(chosen.get("filename") or wanted_name),
        )
        return content, source_url

    def finish_unfinished(self) -> list[DocumentOutcome]:
        """Termine les documents dont l'ecriture comptable a abouti mais dont
        l'archivage, le rappel ou le journal manquent encore."""
        outcomes: list[DocumentOutcome] = []
        for row in store.list_unfinished(self._db_path, self._chat_id):
            try:
                outcomes.append(self.resume(row))
            except Exception as exc:  # noqa: BLE001 - la reprise ne bloque jamais le cycle
                logger.warning("Reprise impossible (%s): %s", row["doc_key"][:12], exc)
        return outcomes

    def resume(self, row: dict[str, Any]) -> DocumentOutcome:
        """Reprend UN document exactement la ou il s'est arrete."""
        file, source_url = self.materialize(row)
        message = {
            "messageId": row["gmail_message_id"],
            "subject": "",
            "sender": "",
        }
        return self.pipeline.process_document(
            file, message,
            attachment_id=str(row.get("attachment_id") or ""),
            source_url=source_url,
            parent_attachment_id=str(row.get("parent_attachment_id") or ""),
            parent_filename=str(row.get("parent_filename") or ""),
            local_path=str(row.get("local_path") or ""),
        )

    def retry_pending(self) -> list[DocumentOutcome]:
        """Relance TOUT ce qui est reste en plan pour ce client.

        Reservee au proprietaire du chat par la commande qui l'appelle.
        Ne force aucune ecriture : un document qui exige encore une decision
        humaine reste en attente, il retrouve seulement des boutons vivants.
        Les documents deja ecrits sont proteges par leur etat : la reprise
        termine les etapes manquantes, elle ne reecrit aucune ligne.
        """
        store.ensure_schema(self._db_path)
        outcomes: list[DocumentOutcome] = []
        seen: set[str] = set()
        rows = (
            store.list_unfinished(self._db_path, self._chat_id)
            + store.list_pending_review(self._db_path, self._chat_id)
        )
        for row in rows:
            if row["doc_key"] in seen:
                continue
            seen.add(row["doc_key"])
            try:
                outcomes.append(self.resume(row))
            except MailWorkerError as exc:
                logger.warning("Reprise refusee (%s): %s", row["doc_key"][:12], exc)
                outcomes.append(
                    DocumentOutcome(
                        doc_key=row["doc_key"],
                        filename=str(row.get("filename") or ""),
                        action=ACTION_REVIEW, error=str(exc),
                        reasons=[str(exc)],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - un document ne bloque pas les autres
                logger.exception("Reprise en echec (%s)", row["doc_key"][:12])
                outcomes.append(
                    DocumentOutcome(
                        doc_key=row["doc_key"],
                        filename=str(row.get("filename") or ""),
                        action=ACTION_REVIEW, error=str(exc),
                        reasons=[f"erreur technique : {exc}"],
                    )
                )
        return outcomes

def already_written_message(row: dict[str, Any]) -> str:
    """Reponse a un second clic sur "Valider". Rien n'est reecrit."""
    reference = row.get("stable_id") or row.get("numero") or row.get("filename") or ""
    parts = [f"Ce document ({reference}) est deja enregistre. Rien n'a ete duplique."]
    if row.get("tab") and row.get("row_index"):
        parts.append(f"Onglet {row['tab']} ligne {row['row_index']}.")
    if row.get("drive_link"):
        parts.append(f"Drive : {row['drive_link']}")
    return " ".join(parts)


def _internal_date(message: dict[str, Any]) -> int:
    """Date de reception en epoch secondes, quelle que soit sa forme."""
    raw = message.get("internalDate") or message.get("messageTimestamp") or ""
    if isinstance(raw, (int, float)):
        value = int(raw)
        return value // 1000 if value > 10_000_000_000 else value
    text = str(raw).strip()
    if text.isdigit():
        value = int(text)
        return value // 1000 if value > 10_000_000_000 else value
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


# --- messages Telegram ----------------------------------------------------

def _money(value, devise: str) -> str:
    if value is None:
        return "-"
    return f"{value} {devise}".strip()


def format_outcome(outcome: DocumentOutcome, *, prefix: str = "") -> str:
    """Detail d'UN document importe."""
    lines = [prefix] if prefix else []
    lines.append(f"{outcome.type_label} {outcome.numero or ''}".strip())
    if outcome.tiers:
        lines.append(f"- Tiers    : {outcome.tiers}")
    if outcome.montant_ht is not None or outcome.montant_ttc is not None:
        lines.append(f"- HT       : {_money(outcome.montant_ht, outcome.devise)}")
        lines.append(f"- TVA      : {_money(outcome.montant_tva, outcome.devise)}")
        lines.append(f"- TTC      : {_money(outcome.montant_ttc, outcome.devise)}")
    if outcome.tab:
        lines.append(f"- Onglet   : {outcome.tab} ligne {outcome.row_index}")
    if outcome.drive_link:
        lines.append(f"- Drive    : {outcome.drive_link}")
    if outcome.echeance:
        lines.append(f"- Echeance : {outcome.echeance}")
    if outcome.calendar_event:
        lines.append("- Rappel Calendar cree")
    for warning in outcome.warnings:
        lines.append(f"- A completer : {warning}")
    return f"{NL}".join(lines)


def build_summary(summary: MailSummary) -> str:
    """Resume compact d'un email, tel qu'il arrive dans Telegram."""
    total = len(summary.notified_outcomes)
    head = [
        f"Email traite : {summary.subject or '(sans objet)'}",
        "",
        f"Documents trouves        : {total}",
        f"Importes en comptabilite : {len(summary.imported)}",
        f"Classes sans ecriture    : {len(summary.classified)}",
        f"Doublons ignores         : {summary.count(ACTION_DUPLICATE)}",
        f"A valider                : {len(summary.to_review)}",
        f"En erreur                : {len(summary.errors) + len(summary.notified_rejected)}",
    ]
    if summary.truncated:
        # En TETE du resume, avant tout detail : une archive tronquee est
        # une perte de documents, pas une ligne de plus dans une liste.
        head.insert(1, "")
        head.insert(
            2,
            f"ATTENTION : {summary.truncated} document(s) de l'archive n'ont "
            f"PAS ete lus (limite de fichiers atteinte).",
        )

    body: list[str] = []
    detailles = summary.imported + summary.classified
    # Resume COMPACT : au-dela de ce seuil, on donne le compte et non la
    # liste. Un email de 38 documents produisait un message que Telegram
    # refusait, et l'echec faisait tout disparaitre - resume compris.
    for outcome in detailles[:MAX_DETAIL_LINES]:
        body.append("")
        body.append(format_outcome(outcome))
    if len(detailles) > MAX_DETAIL_LINES:
        body.append("")
        body.append(
            f"... et {len(detailles) - MAX_DETAIL_LINES} autre(s) document(s) "
            f"importe(s) ou classe(s). Detail complet dans 14_IMPORTS_LOG."
        )
    for outcome in summary.notified_outcomes:
        if outcome.action == ACTION_DUPLICATE:
            body.append("")
            body.append(
                f"{outcome.type_label} {outcome.numero or outcome.filename} : deja importe"
                + (f" ({outcome.stable_id})" if outcome.stable_id else "")
                + ". Rien n'a ete ecrit."
            )
        elif outcome.action == ACTION_UNKNOWN:
            body.append("")
            body.append(
                f"{outcome.filename} : type non reconnu. Depose dans Drive / A verifier."
            )
    rejets = summary.notified_rejected
    for name, reason in rejets[:MAX_DETAIL_LINES]:
        body.append("")
        body.append(f"{name} : ignore ({reason})")
    if len(rejets) > MAX_DETAIL_LINES:
        body.append("")
        body.append(f"... et {len(rejets) - MAX_DETAIL_LINES} autre(s) rejet(s).")
    return f"{NL}".join(head + body)


def build_review_message(outcome: DocumentOutcome) -> str:
    """Message d'un document ECARTE de la comptabilite.

    Purement informatif : il n'y a plus rien a valider dans Telegram.
    Le document est deja inscrit dans 21_A_VERIFIER avec son motif.
    """
    doc = outcome.document
    lines = [
        f"Document a verifier : {outcome.type_label} {outcome.numero or ''}".strip(),
        f"Fichier : {outcome.filename}",
        "",
        "Valeurs detectees :",
        f"- Tiers    : {outcome.tiers or 'non trouve'}",
        f"- Date     : {doc.date_document if doc else 'non trouvee'}",
        f"- HT       : {_money(outcome.montant_ht, outcome.devise)}",
        f"- TVA      : {_money(outcome.montant_tva, outcome.devise)}",
        f"- TTC      : {_money(outcome.montant_ttc, outcome.devise)}",
    ]
    if outcome.echeance:
        lines.append(f"- Echeance : {outcome.echeance}")
    if doc and doc.text_source == "ocr":
        lines.append("- Lecture  : OCR (couche texte absente)")
    lines += ["", "Motif de la mise a l'ecart :"]
    lines += [f"- {reason}" for reason in outcome.reasons]
    # Information EXACTE : la piece est deja archivee, c'est l'ecriture
    # comptable - et elle seule - qui attend une decision. L'ancien message
    # affirmait le contraire.
    lines += ["", "Etat du document :"]
    if outcome.drive_link:
        lines.append(f"- Archive dans Drive / A verifier : {outcome.drive_link}")
    else:
        lines.append("- Archivage Drive a terminer au prochain cycle")
    lines.append("- Trace ecrite dans 14_IMPORTS_LOG")
    lines.append(f"- Inscrit en rouge dans {TAB_REVIEW}, avec le motif ci-dessus")
    lines.append(
        "- Aucune ligne comptable, aucun rappel Calendar : ces montants "
        "n'entrent ni dans les totaux, ni dans le Dashboard, ni dans la TVA"
    )
    lines.append(
        "- Rien a valider ici : corrige la ligne dans le classeur, ou "
        "demande-moi la correction en clair"
    )
    return f"{NL}".join(lines)
