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

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app import doc_store as store
from app.attachments import ZipLimits, collect_documents, idempotency_key
from app.doc_pipeline import DocumentOutcome, DocumentPipeline
from app.doc_policy import ACTION_AUTO, ACTION_DUPLICATE, ACTION_REVIEW, ACTION_UNKNOWN
from app.doc_types import LABELS

logger = logging.getLogger("demo_bot.mail_worker")

COMPOSIO_BASE_URL = "https://backend.composio.dev"
COMPOSIO_TOOLS_EXECUTE_PATH = "/api/v3.1/tools/execute/{tool_slug}"
_REQUEST_TIMEOUT_SECONDS = 60.0
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Telegram limite `callback_data` a 64 octets : le prefixe court de la cle
# d'idempotence suffit a retrouver le document sans risque de collision.
CALLBACK_CONFIRM_PREFIX = "dok:"
CALLBACK_REFUSE_PREFIX = "dno:"
CALLBACK_KEY_LENGTH = 24


class MailWorkerError(RuntimeError):
    """Erreur destinee aux logs / au client, jamais porteuse de secret."""


@dataclass
class MailSummary:
    """Bilan d'UN email, tel qu'il sera resume dans Telegram."""

    message_id: str
    subject: str
    sender: str
    outcomes: list[DocumentOutcome] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for o in self.outcomes if o.action == action)

    @property
    def imported(self) -> list[DocumentOutcome]:
        return [o for o in self.outcomes if o.action == ACTION_AUTO and o.accounting]

    @property
    def classified(self) -> list[DocumentOutcome]:
        return [o for o in self.outcomes if o.action == ACTION_AUTO and not o.accounting]

    @property
    def to_review(self) -> list[DocumentOutcome]:
        return [o for o in self.outcomes if o.action == ACTION_REVIEW]

    @property
    def errors(self) -> list[DocumentOutcome]:
        return [o for o in self.outcomes if o.error and o.action != ACTION_AUTO]


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
    ) -> None:
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
        self._client = None
        self._pipeline: DocumentPipeline | None = None

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
        """Appel Composio, isole par `user_id = telegram_<chat_id>`."""
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
            logger.warning("Outil '%s' en echec (user=%s)", slug, self.user_id)
            raise MailWorkerError(f"L'outil '{slug}' a echoue.")
        return result.get("data") or {}

    # -- curseur -----------------------------------------------------------

    def cursor(self) -> dict[str, Any]:
        store.ensure_schema(self._db_path)
        return store.get_or_init_cursor(
            self._db_path, self._chat_id, int(datetime.now(timezone.utc).timestamp())
        )

    def effective_query(self) -> str:
        return f"{self._query} after:{store.query_floor(self.cursor())}"

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

    @staticmethod
    def attachments_of(message: dict[str, Any]) -> list[dict[str, Any]]:
        """Toutes les pieces jointes exploitables : PDF et archives ZIP."""
        keep = []
        for att in message.get("attachmentList") or []:
            name = str(att.get("filename") or "").lower()
            mime = str(att.get("mimeType") or "").lower()
            if name.endswith(".pdf") or name.endswith(".zip") or mime in (
                "application/pdf", "application/zip", "application/x-zip-compressed",
            ):
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
        summaries: list[MailSummary] = []
        resumed = self.finish_unfinished()
        if resumed:
            summaries.append(
                MailSummary(message_id="(reprises)", subject="Reprise d'imports interrompus",
                            sender="", outcomes=resumed)
            )
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
            if summary.outcomes or summary.rejected:
                summaries.append(summary)
        if newest:
            store.advance_cursor(self._db_path, self._chat_id, newest)
        return summaries

    def process_message(self, message_id: str) -> tuple[MailSummary, int]:
        """Traite toutes les pieces jointes d'un email, independamment."""
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
            for file in report.files:
                try:
                    summary.outcomes.append(
                        self.pipeline.process_document(
                            file, message, attachment_id=attachment_id, source_url=source_url
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - un document ne bloque pas les autres
                    logger.exception("Document %s en echec", file.display_name)
                    summary.outcomes.append(
                        DocumentOutcome(
                            doc_key=idempotency_key(
                                self.user_id, message_id, attachment_id, file.sha256
                            ),
                            filename=file.display_name,
                            action=ACTION_REVIEW,
                            error=str(exc),
                            reasons=[f"erreur technique : {exc}"],
                        )
                    )
        return summary, internal_date

    def finish_unfinished(self) -> list[DocumentOutcome]:
        """Termine les documents dont l'ecriture comptable a abouti mais dont
        l'archivage, le rappel ou le journal manquent encore."""
        outcomes: list[DocumentOutcome] = []
        for row in store.list_unfinished(self._db_path, self._chat_id):
            try:
                message = self.fetch_message(row["gmail_message_id"])
                message.setdefault("messageId", row["gmail_message_id"])
                attachment = next(
                    (a for a in self.attachments_of(message)
                     if str(a.get("attachmentId")) == row["attachment_id"]),
                    None,
                )
                if attachment is None:
                    continue
                content, source_url = self.download(row["gmail_message_id"], attachment)
                report = collect_documents(
                    str(attachment.get("filename") or ""), content, limits=self._zip_limits
                )
                file = next((f for f in report.files if f.sha256 == row["file_sha256"]), None)
                if file is None:
                    continue
                outcomes.append(
                    self.pipeline.process_document(
                        file, message, attachment_id=row["attachment_id"],
                        source_url=source_url,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - la reprise ne bloque jamais le cycle
                logger.warning("Reprise impossible (%s): %s", row["doc_key"][:12], exc)
        return outcomes

    # -- validation humaine ------------------------------------------------

    def confirm(self, short_key: str) -> str:
        """Ecrit un document apres validation humaine."""
        row = store.find_by_key_prefix(self._db_path, self._chat_id, short_key)
        if row is None:
            raise MailWorkerError("Document introuvable.")
        if row["state"] == store.COMPLETED:
            return "Ce document a deja ete enregistre. Rien n'a ete duplique."
        message = self.fetch_message(row["gmail_message_id"])
        message.setdefault("messageId", row["gmail_message_id"])
        attachment = next(
            (a for a in self.attachments_of(message)
             if str(a.get("attachmentId")) == row["attachment_id"]), None
        )
        if attachment is None:
            raise MailWorkerError("Piece jointe introuvable dans l'email d'origine.")
        content, source_url = self.download(row["gmail_message_id"], attachment)
        report = collect_documents(str(attachment.get("filename") or ""), content,
                                   limits=self._zip_limits)
        file = next((f for f in report.files if f.sha256 == row["file_sha256"]), None)
        if file is None:
            raise MailWorkerError("Fichier introuvable dans la piece jointe.")
        store.set_state(self._db_path, row["doc_key"], store.VALIDATED)
        outcome = self.pipeline.process_document(
            file, message, attachment_id=row["attachment_id"], source_url=source_url,
            forced=True,
        )
        return format_outcome(outcome, prefix="Document valide et enregistre")

    def refuse(self, short_key: str) -> str:
        row = store.find_by_key_prefix(self._db_path, self._chat_id, short_key)
        if row is None:
            raise MailWorkerError("Document introuvable.")
        if row["state"] == store.COMPLETED:
            return "Ce document a deja ete enregistre ; il ne peut plus etre refuse ici."
        store.set_state(self._db_path, row["doc_key"], store.SKIPPED, error="refuse par le client")
        return (
            f"Document {row['numero'] or row['filename']} refuse. "
            "Rien n'a ete ecrit dans Sheets, Drive ni Calendar."
        )


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
    return "\n".join(lines)


def build_summary(summary: MailSummary) -> str:
    """Resume compact d'un email, tel qu'il arrive dans Telegram."""
    total = len(summary.outcomes)
    head = [
        f"Email traite : {summary.subject or '(sans objet)'}",
        "",
        f"Documents trouves        : {total}",
        f"Importes en comptabilite : {len(summary.imported)}",
        f"Classes sans ecriture    : {len(summary.classified)}",
        f"Doublons ignores         : {summary.count(ACTION_DUPLICATE)}",
        f"A valider                : {len(summary.to_review)}",
        f"En erreur                : {len(summary.errors) + len(summary.rejected)}",
    ]
    body: list[str] = []
    for outcome in summary.imported + summary.classified:
        body.append("")
        body.append(format_outcome(outcome))
    for outcome in summary.outcomes:
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
    for name, reason in summary.rejected:
        body.append("")
        body.append(f"{name} : ignore ({reason})")
    return "\n".join(head + body)


def build_review_message(outcome: DocumentOutcome) -> str:
    """Message d'un document qui exige une validation humaine."""
    doc = outcome.document
    lines = [
        f"Validation requise : {outcome.type_label} {outcome.numero or ''}".strip(),
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
    lines += ["", "Motif de la validation :"]
    lines += [f"- {reason}" for reason in outcome.reasons]
    lines += ["", "Rien n'a ete ecrit dans Sheets, Drive ni Calendar."]
    return "\n".join(lines)
