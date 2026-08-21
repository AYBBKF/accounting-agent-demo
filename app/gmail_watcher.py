"""Worker Gmail : detection automatique des factures [XBLASTE].

Boucle de fond demarree avec le bot. Toutes les `poll_seconds` secondes,
elle interroge Gmail via la connexion Composio DU CLIENT
(user_id = "telegram_<chat_id>"), telecharge les pieces jointes PDF,
extrait les champs de facon DETERMINISTE (app/invoice_pdf.py, aucun
modele de langage) et envoie un apercu dans Telegram.

Garde-fous :
  - anti-doublon durable : le message_id Gmail est reserve dans SQLite
    avant tout traitement (voir db.claim_gmail_message) ;
  - AUCUNE ecriture Google Sheets ni Drive avant confirmation explicite
    du client via les boutons Telegram ;
  - Gmail reste en lecture : aucun envoi, aucune suppression ;
  - aucun token ni cle API n'est journalise.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.db import claim_gmail_message, get_gmail_message, set_gmail_message_status
from app.invoice_pdf import ExtractedInvoice, InvoicePdfError, extract_from_pdf_bytes

logger = logging.getLogger("demo_bot.gmail_watcher")

COMPOSIO_BASE_URL = "https://backend.composio.dev"
COMPOSIO_TOOLS_EXECUTE_PATH = "/api/v3.1/tools/execute/{tool_slug}"
_REQUEST_TIMEOUT_SECONDS = 60.0
_MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

CALLBACK_CONFIRM_PREFIX = "xbok:"
CALLBACK_REFUSE_PREFIX = "xbno:"

# En-tetes reels des onglets factures du classeur de demo. Les colonnes
# calculees par formule dans le Sheet sont volontairement laissees vides.
_INVOICE_HEADERS = [
    "ID", "Date", "Numéro facture", "ID Fournisseur", "Fournisseur", "Description",
    "Montant HT (facture)", "Taux TVA (%)", "Montant TVA (facture)",
    "Montant TTC (facture)", "Montant TTC théorique (formule)", "Écart TTC (formule)",
    "Doublon numéro? (formule)", "Échéance", "Montant payé", "Statut",
    "Jours de retard (formule)",
]
_SALES_HEADERS = [
    h.replace("ID Fournisseur", "ID Client").replace("Fournisseur", "Client")
    if h in ("ID Fournisseur", "Fournisseur") else h
    for h in _INVOICE_HEADERS
]


class GmailWatcherError(RuntimeError):
    """Erreur destinee aux logs / au client, jamais porteuse de secret."""


@dataclass
class PendingInvoice:
    message_id: str
    thread_id: str
    subject: str
    sender: str
    received_at: str
    attachment_name: str
    fields: ExtractedInvoice
    scope: str          # "purchases" ou "sales"

    @property
    def target_tab_hint(self) -> str:
        return "factures d'achat" if self.scope == "purchases" else "factures de vente"


def _fmt(value: Any) -> str:
    if value is None:
        return "non trouve"
    if isinstance(value, Decimal):
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return str(value)


def build_preview(pending: PendingInvoice) -> str:
    """Apercu envoye dans Telegram AVANT toute ecriture."""
    f = pending.fields
    devise = f.devise or ""
    lines = [
        "Nouvelle facture detectee par email.",
        "",
        f"Objet   : {pending.subject}",
        f"De      : {pending.sender}",
        f"Fichier : {pending.attachment_name}",
        "",
        "Donnees extraites du PDF :",
        f"- Numero      : {_fmt(f.numero)}",
        f"- Date        : {_fmt(f.date_facture)}",
        f"- Echeance    : {_fmt(f.date_echeance)}",
        f"- Fournisseur : {_fmt(f.fournisseur)}",
        f"- Client      : {_fmt(f.client)}",
        f"- HT          : {_fmt(f.montant_ht)} {devise}".rstrip(),
        f"- TVA {_fmt(f.taux_tva)} %    : {_fmt(f.montant_tva)} {devise}".rstrip(),
        f"- TTC         : {_fmt(f.montant_ttc)} {devise}".rstrip(),
        f"- Statut      : {_fmt(f.statut)}",
        f"- Paiement    : {_fmt(f.mode_paiement)}",
        "",
        f"Destination proposee : {pending.target_tab_hint}.",
    ]
    if f.missing:
        lines += ["", f"Champs introuvables : {', '.join(f.missing)} (non devines)."]
    if f.anomalies:
        lines += ["", "Anomalies detectees :"] + [f"- {a}" for a in f.anomalies]
    lines += ["", "Rien n'a encore ete ecrit. Confirme pour enregistrer."]
    return "\n".join(lines)


class GmailWatcher:
    """Interroge Gmail periodiquement et prepare les factures a confirmer."""

    def __init__(
        self,
        api_key: str,
        chat_id: int,
        db_path: str,
        spreadsheet_id: str = "",
        query: str = 'subject:"[XBLASTE]" has:attachment filename:pdf',
        poll_seconds: int = 60,
        company_name: str = "X BLASTE",
        drive_folder: str = "XBLASTE - Factures",
        max_per_cycle: int = 5,
    ) -> None:
        self._api_key = api_key
        self._chat_id = chat_id
        self._db_path = db_path
        self._spreadsheet_id = spreadsheet_id
        self._query = query
        self._poll_seconds = poll_seconds
        self._company_name = company_name
        self._drive_folder = drive_folder
        self._max_per_cycle = max_per_cycle
        self._client = None

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

    # -- transport ---------------------------------------------------------

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise GmailWatcherError("COMPOSIO_API_KEY manquant.")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise GmailWatcherError(f"Dependance httpx manquante: {exc}") from exc
        try:
            self._client = httpx.Client(
                base_url=COMPOSIO_BASE_URL,
                headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - l'en-tete porte la cle API
            logger.warning("Client HTTP Composio non initialisable: %s", type(exc).__name__)
            raise GmailWatcherError("Client HTTP Composio non initialisable.") from exc
        return self._client

    def _execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        path = COMPOSIO_TOOLS_EXECUTE_PATH.format(tool_slug=slug)
        try:
            response = client.post(path, json={"arguments": arguments, "user_id": self.user_id})
            response.raise_for_status()
            result = response.json()
        except Exception as exc:  # noqa: BLE001 - jamais de secret dans le message
            logger.warning("Outil '%s' injoignable (user=%s): %s", slug, self.user_id, exc)
            raise GmailWatcherError(f"Appel '{slug}' impossible.") from exc
        if not result.get("successful", False):
            logger.warning("Outil '%s' en echec (user=%s)", slug, self.user_id)
            raise GmailWatcherError(f"L'outil '{slug}' a echoue.")
        return result.get("data") or {}

    # -- lecture Gmail -----------------------------------------------------

    def search_messages(self) -> list[dict[str, Any]]:
        data = self._execute(
            "GMAIL_FETCH_EMAILS", {"query": self._query, "max_results": self._max_per_cycle}
        )
        return data.get("messages") or []

    def fetch_message(self, message_id: str) -> dict[str, Any]:
        return self._execute(
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": message_id, "format": "full"}
        )

    @staticmethod
    def first_pdf_attachment(message: dict[str, Any]) -> dict[str, Any] | None:
        for att in message.get("attachmentList") or []:
            name = str(att.get("filename") or "")
            mime = str(att.get("mimeType") or "")
            if name.lower().endswith(".pdf") or mime == "application/pdf":
                return att
        return None

    def download_attachment(self, message_id: str, attachment: dict[str, Any]) -> bytes:
        data = self._execute(
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
                raise GmailWatcherError("Reponse de piece jointe illisible.") from exc
        if not isinstance(file_info, dict):
            raise GmailWatcherError("Piece jointe absente de la reponse Gmail.")
        url = file_info.get("s3url") or file_info.get("url")
        if not url:
            raise GmailWatcherError("Aucune URL de telechargement pour la piece jointe.")
        try:
            import httpx

            response = httpx.get(url, timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
            response.raise_for_status()
            content = response.content
        except Exception as exc:  # noqa: BLE001 - l'URL signee n'est jamais journalisee
            logger.warning("Telechargement de la piece jointe impossible (message=%s)", message_id)
            raise GmailWatcherError("Telechargement de la piece jointe impossible.") from exc
        if len(content) > _MAX_ATTACHMENT_BYTES:
            raise GmailWatcherError(
                f"Piece jointe trop volumineuse ({len(content)} octets), ignoree."
            )
        return content

    # -- classement ventes / achats ---------------------------------------

    def decide_scope(self, fields: ExtractedInvoice) -> str:
        """Une facture dont NOUS sommes le client est un achat ; une facture
        que nous emettons est une vente. En cas de doute : achat (le cas le
        plus frequent pour une facture recue par email)."""
        company = self._company_name.strip().upper()
        client = (fields.client or "").strip().upper()
        supplier = (fields.fournisseur or "").strip().upper()
        if company and company in client:
            return "purchases"
        if company and company in supplier:
            return "sales"
        return "purchases"

    # -- cycle -------------------------------------------------------------

    def process_once(self) -> list[PendingInvoice]:
        """Un tour de boucle. Retourne les factures nouvellement detectees,
        deja enregistrees en base avec le statut 'pending'."""
        pendings: list[PendingInvoice] = []
        messages = self.search_messages()
        logger.info(
            "Cycle Gmail (user=%s): %d message(s) correspondant a la requete",
            self.user_id, len(messages),
        )
        for meta in messages:
            message_id = meta.get("messageId") or meta.get("id")
            if not message_id:
                continue
            if get_gmail_message(self._db_path, message_id):
                logger.debug("Message %s deja traite, ignore", message_id)
                continue
            try:
                pending = self._process_message(message_id)
            except (GmailWatcherError, InvoicePdfError) as exc:
                logger.warning("Message %s ignore: %s", message_id, exc)
                claim_gmail_message(
                    self._db_path, message_id, self._chat_id,
                    status="skipped", payload=json.dumps({"raison": str(exc)}),
                )
                continue
            if pending is not None:
                pendings.append(pending)
        return pendings

    def _process_message(self, message_id: str) -> PendingInvoice | None:
        message = self.fetch_message(message_id)
        attachment = self.first_pdf_attachment(message)
        if attachment is None:
            raise GmailWatcherError("aucune piece jointe PDF")

        # Reservation AVANT tout traitement couteux : garantit qu'un
        # redemarrage ou un cycle concurrent ne retraitera pas ce message.
        if not claim_gmail_message(
            self._db_path, message_id, self._chat_id,
            thread_id=message.get("threadId", ""),
            subject=message.get("subject", ""),
            sender=message.get("sender", ""),
            received_at=message.get("messageTimestamp", ""),
            attachment_name=attachment.get("filename", ""),
            status="processing",
        ):
            logger.debug("Message %s deja reserve par un autre cycle", message_id)
            return None

        content = self.download_attachment(message_id, attachment)
        logger.info(
            "Piece jointe telechargee (message=%s, %d octets)", message_id, len(content)
        )
        fields = extract_from_pdf_bytes(content)
        scope = self.decide_scope(fields)
        logger.info(
            "Extraction terminee (message=%s, numero=%s, complet=%s, anomalies=%d, scope=%s)",
            message_id, fields.numero, fields.is_complete, len(fields.anomalies), scope,
        )

        pending = PendingInvoice(
            message_id=message_id,
            thread_id=message.get("threadId", ""),
            subject=message.get("subject", ""),
            sender=message.get("sender", ""),
            received_at=message.get("messageTimestamp", ""),
            attachment_name=attachment.get("filename", ""),
            fields=fields,
            scope=scope,
        )
        self._save_pending(pending)
        return pending

    def _save_pending(self, pending: PendingInvoice) -> None:
        from app.db import connect

        payload = json.dumps(
            {
                "numero": pending.fields.numero,
                "date_facture": pending.fields.date_facture.isoformat() if pending.fields.date_facture else None,
                "date_echeance": pending.fields.date_echeance.isoformat() if pending.fields.date_echeance else None,
                "fournisseur": pending.fields.fournisseur,
                "client": pending.fields.client,
                "montant_ht": str(pending.fields.montant_ht) if pending.fields.montant_ht is not None else None,
                "taux_tva": str(pending.fields.taux_tva) if pending.fields.taux_tva is not None else None,
                "montant_tva": str(pending.fields.montant_tva) if pending.fields.montant_tva is not None else None,
                "montant_ttc": str(pending.fields.montant_ttc) if pending.fields.montant_ttc is not None else None,
                "devise": pending.fields.devise,
                "statut": pending.fields.statut,
                "mode_paiement": pending.fields.mode_paiement,
                "scope": pending.scope,
                "attachment_name": pending.attachment_name,
                "missing": pending.fields.missing,
                "anomalies": pending.fields.anomalies,
            },
            ensure_ascii=False,
        )
        with connect(self._db_path) as conn:
            conn.execute(
                "UPDATE gmail_processed_emails SET status='pending', numero=?, payload=? "
                "WHERE message_id=?",
                (pending.fields.numero or "", payload, pending.message_id),
            )
            conn.commit()

    # -- apres confirmation du client --------------------------------------

    def confirm(self, message_id: str) -> str:
        """Ecrit reellement la facture dans Sheets puis archive le PDF dans
        Drive. Appele UNIQUEMENT apres clic sur 'Confirmer l'ecriture'."""
        row = get_gmail_message(self._db_path, message_id)
        if row is None:
            raise GmailWatcherError("Facture introuvable (message inconnu).")
        if row["status"] == "confirmed":
            return "Cette facture a deja ete enregistree. Rien n'a ete duplique."
        if row["status"] == "refused":
            return "Cette facture avait ete refusee. Rien n'a ete ecrit."
        data = json.loads(row["payload"] or "{}")

        steps: list[str] = []
        tab = self._write_to_sheet(data)
        steps.append(f"ecrite dans l'onglet {tab}")
        logger.info("Facture %s ecrite dans %s (message=%s)", data.get("numero"), tab, message_id)

        try:
            archived = self._archive_to_drive(message_id, row["attachment_name"] or "facture.pdf")
            steps.append(f"PDF archive dans Drive ({archived})")
            logger.info("PDF archive dans Drive (message=%s)", message_id)
        except GmailWatcherError as exc:
            steps.append(f"archivage Drive impossible : {exc}")
            logger.warning("Archivage Drive impossible (message=%s): %s", message_id, exc)

        set_gmail_message_status(self._db_path, message_id, "confirmed")
        return "Facture {} enregistree :\n- {}".format(
            data.get("numero") or message_id, "\n- ".join(steps)
        )

    def refuse(self, message_id: str) -> str:
        row = get_gmail_message(self._db_path, message_id)
        if row is None:
            raise GmailWatcherError("Facture introuvable (message inconnu).")
        if row["status"] == "confirmed":
            return "Cette facture a deja ete enregistree ; elle ne peut plus etre refusee ici."
        set_gmail_message_status(self._db_path, message_id, "refused")
        logger.info("Facture refusee par le client (message=%s)", message_id)
        return (
            f"Facture {row['numero'] or message_id} refusee. "
            "Rien n'a ete ecrit dans Sheets ni dans Drive."
        )

    def _write_to_sheet(self, data: dict[str, Any]) -> str:
        if not self._spreadsheet_id:
            raise GmailWatcherError("GOOGLE_SHEET_ID manquant : ecriture impossible.")
        scope = data.get("scope", "purchases")
        tabs = self._list_tabs()
        tab = self._pick_invoice_tab(tabs, scope)
        headers = _INVOICE_HEADERS if scope == "purchases" else _SALES_HEADERS
        numero = data.get("numero") or ""
        devise = data.get("devise") or ""

        def money(key: str) -> str:
            value = data.get(key)
            return f"{value} {devise}".strip() if value is not None else ""

        row = [
            numero,                                   # ID (cle d'idempotence)
            data.get("date_facture") or "",
            numero,
            "",                                       # ID tiers : inconnu depuis le PDF
            data.get("fournisseur") if scope == "purchases" else data.get("client") or "",
            f"Import email - {data.get('attachment_name') or ''}".strip(" -"),
            money("montant_ht"),
            f"{data.get('taux_tva')}%" if data.get("taux_tva") is not None else "",
            money("montant_tva"),
            money("montant_ttc"),
            "", "", "",                               # colonnes formules du Sheet
            data.get("date_echeance") or "",
            "",                                       # montant paye : inconnu
            data.get("statut") or "",
            "",                                       # jours de retard : formule
        ]
        self._execute(
            "GOOGLESHEETS_UPSERT_ROWS",
            {
                "spreadsheetId": self._spreadsheet_id,
                "sheetName": tab,
                "headers": headers,
                "rows": [row],
                "keyColumn": headers[0],
            },
        )
        return tab

    def _list_tabs(self) -> list[str]:
        data = self._execute(
            "GOOGLESHEETS_GET_SPREADSHEET_INFO", {"spreadsheet_id": self._spreadsheet_id}
        )
        return [
            t for t in (
                (s.get("properties") or {}).get("title", "") for s in data.get("sheets", [])
            ) if t
        ]

    @staticmethod
    def _pick_invoice_tab(tabs: list[str], scope: str) -> str:
        import re

        wanted = r"factures?_?achats?" if scope == "purchases" else r"factures?_?ventes?"
        for tab in tabs:
            if re.search(wanted, tab, re.I):
                return tab
        raise GmailWatcherError(
            f"Aucun onglet correspondant a '{scope}' dans le classeur "
            f"(onglets : {', '.join(tabs) if tabs else 'aucun'})."
        )

    def _archive_to_drive(self, message_id: str, filename: str) -> str:
        folder_id = self._ensure_drive_folder()
        message = self.fetch_message(message_id)
        attachment = self.first_pdf_attachment(message)
        if attachment is None:
            raise GmailWatcherError("piece jointe introuvable pour l'archivage")
        data = self._execute(
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

            file_info = ast.literal_eval(file_info)
        url = (file_info or {}).get("s3url")
        if not url:
            raise GmailWatcherError("URL de la piece jointe indisponible")
        args: dict[str, Any] = {"file_url": url, "file_name": filename}
        if folder_id:
            args["folder_id"] = folder_id
        self._execute("GOOGLEDRIVE_UPLOAD_FROM_URL", args)
        return self._drive_folder

    def _ensure_drive_folder(self) -> str:
        try:
            found = self._execute("GOOGLEDRIVE_FIND_FOLDER", {"folder_name": self._drive_folder})
            for key in ("files", "folders", "items"):
                items = found.get(key) or []
                if items:
                    return items[0].get("id", "")
        except GmailWatcherError:
            pass
        try:
            created = self._execute(
                "GOOGLEDRIVE_CREATE_FOLDER", {"folder_name": self._drive_folder}
            )
            return created.get("id", "") or (created.get("file") or {}).get("id", "")
        except GmailWatcherError:
            logger.info("Dossier Drive non cree, archivage a la racine")
            return ""
