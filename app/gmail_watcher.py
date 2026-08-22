"""Worker Gmail : detection automatique des factures recues par email.

Boucle de fond demarree avec le bot. Toutes les `poll_seconds` secondes,
elle interroge Gmail via la connexion Composio DU CLIENT
(user_id = "telegram_<chat_id>"), telecharge les pieces jointes PDF,
extrait les champs de facon DETERMINISTE (app/invoice_pdf.py, aucun
modele de langage) et applique la politique d'import (app/invoice_policy.py).

Comportement :
  - facture lisible, complete, coherente et non ambigue -> import
    AUTOMATIQUE, sans confirmation, suivi d'une notification Telegram ;
  - doute (champ illisible, HT + TVA != TTC, ICE manquant, fournisseur
    ambigu, plusieurs valeurs possibles, avoir, doublon incertain) ->
    apercu + boutons de validation, et AUCUNE ecriture ;
  - doublon certain -> aucune ecriture, information seule.

Garde-fous :
  - anti-doublon durable a deux niveaux : le message_id Gmail (technique)
    et le couple (ICE fournisseur + numero de facture) (metier) ;
  - Gmail reste en lecture : aucun envoi, aucune suppression ;
  - aucun token ni cle API n'est journalise.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.db import (
    claim_gmail_message,
    claim_invoice_fingerprint,
    get_gmail_message,
    get_invoice_fingerprint,
    get_or_init_gmail_cursor,
    list_partial_imports,
    release_invoice_fingerprint,
    set_gmail_message_status,
    update_invoice_fingerprint,
)
from app.invoice_pdf import (
    ExtractedInvoice,
    InvoiceLine,
    InvoicePdfError,
    extract_from_pdf_bytes,
)
from app.invoice_policy import (
    ACTION_AUTO,
    ACTION_DUPLICATE,
    ACTION_REVIEW,
    Decision,
    DuplicateState,
    decide_invoice,
    fingerprint,
)
from app.invoice_sheet import (
    DATE_COLUMNS,
    DATE_PATTERN,
    IMPORTS_LOG_TAB,
    LIGNES_HEADERS,
    LIGNES_TAB,
    MONEY_COLUMNS,
    MONEY_PATTERN,
    RATE_COLUMN,
    RATE_PATTERN,
    STATUS_VALUES,
    build_import_log_row,
    build_line_rows,
    build_row_plan,
    next_stable_invoice_id,
    next_supplier_id,
)

logger = logging.getLogger("demo_bot.gmail_watcher")

COMPOSIO_BASE_URL = "https://backend.composio.dev"
COMPOSIO_TOOLS_EXECUTE_PATH = "/api/v3.1/tools/execute/{tool_slug}"
_REQUEST_TIMEOUT_SECONDS = 60.0
_MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

SUPPLIERS_TAB = "03_FOURNISSEURS"
DEFAULT_ROW_BACKGROUND = "#ffffff"

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


def _norm_name(value: str | None) -> str:
    """Comparaison de raisons sociales : casse, accents et ponctuation ignores."""
    import re as _re
    import unicodedata as _ud

    text = _ud.normalize("NFD", value or "")
    text = "".join(c for c in text if _ud.category(c) != "Mn")
    return _re.sub(r"[^A-Z0-9]", "", text.upper())


@dataclass
class SupplierMatch:
    """Fournisseur resolu par ICE (ou a creer)."""

    supplier_id: str = ""
    name: str = ""
    existing: bool = False
    ambiguous: bool = False
    reason: str = ""


@dataclass
class WatchOutcome:
    """Ce que le bot doit faire d'une facture detectee."""

    pending: "PendingInvoice"
    decision: Decision
    message: str
    needs_buttons: bool


class GmailWatcherError(RuntimeError):
    """Erreur destinee aux logs / au client, jamais porteuse de secret."""


class PartialImportError(GmailWatcherError):
    """La ligne comptable a bien ete ecrite, mais une etape posterieure
    (archivage Drive, journal) a echoue. Surtout ne pas retenter l'ecriture :
    le cycle suivant reprendra a l'etape manquante."""


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
        f"- ICE         : {_fmt(f.ice_fournisseur)}",
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
    if f.ambigus:
        lines += ["", f"Champs a plusieurs valeurs possibles : {', '.join(f.ambigus)}."]
    lines += ["", "Rien n'a encore ete ecrit."]
    return "\n".join(lines)


class GmailWatcher:
    """Interroge Gmail periodiquement et prepare les factures a confirmer."""

    def __init__(
        self,
        api_key: str,
        chat_id: int,
        db_path: str,
        spreadsheet_id: str = "",
        query: str = "in:inbox has:attachment filename:pdf",
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

    # -- curseur Gmail durable --------------------------------------------

    def cursor_epoch(self) -> int:
        """Plancher temporel, fixe au PREMIER demarrage et jamais modifie.

        La requete ne filtrant plus sur un marqueur de sujet, sans ce
        plancher le premier cycle importerait toute l'historique de la boite
        de reception. Il est stocke en base : il survit aux redemarrages.
        """
        from datetime import datetime, timezone

        return get_or_init_gmail_cursor(
            self._db_path, self._chat_id, int(datetime.now(timezone.utc).timestamp())
        )

    def effective_query(self) -> str:
        """Requete Gmail reellement envoyee, curseur inclus."""
        return f"{self._query} after:{self.cursor_epoch()}"

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
            "GMAIL_FETCH_EMAILS",
            {"query": self.effective_query(), "max_results": self._max_per_cycle},
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

    def process_once(self) -> list[WatchOutcome]:
        """Un tour de boucle.

        Chaque facture detectee est immediatement soumise a la politique
        d'import : ecriture automatique si elle est certaine, demande de
        validation sinon, information seule si c'est un doublon certain.
        """
        outcomes: list[WatchOutcome] = []
        outcomes.extend(self.finish_partial_imports())
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
                logger.info("Message %s ignore: %s", message_id, exc)
                claim_gmail_message(
                    self._db_path, message_id, self._chat_id,
                    status="skipped", payload=json.dumps({"raison": str(exc)}),
                )
                # Le message avait deja pu etre reserve : INSERT OR IGNORE
                # n'aurait alors rien mis a jour, et il serait retente a
                # chaque cycle.
                set_gmail_message_status(self._db_path, message_id, "skipped")
                continue
            if pending is None:
                continue
            try:
                outcomes.append(self.handle(pending))
            except (GmailWatcherError, InvoicePdfError) as exc:
                logger.warning("Import impossible (message=%s): %s", message_id, exc)
                # Ne jamais ecraser un point de reprise : si la ligne est deja
                # ecrite, le statut 'partial' doit survivre pour que le cycle
                # suivant termine l'import au lieu de le recommencer.
                current = get_gmail_message(self._db_path, message_id) or {}
                if current.get("status") != "partial":
                    set_gmail_message_status(self._db_path, message_id, "pending")
                outcomes.append(
                    WatchOutcome(
                        pending=pending,
                        decision=Decision(action=ACTION_REVIEW, reasons=[str(exc)]),
                        message=(
                            build_preview(pending)
                            + f"\n\nValidation humaine demandee car :\n- {exc}"
                        ),
                        needs_buttons=True,
                    )
                )
        return outcomes

    def finish_partial_imports(self) -> list[WatchOutcome]:
        """Termine les imports interrompus apres l'ecriture comptable.

        Cas typique : Sheets a reussi, Drive a echoue. La ligne existe deja ;
        cette reprise ne fait QUE l'archivage et le journal manquants.
        """
        outcomes: list[WatchOutcome] = []
        for row in list_partial_imports(self._db_path, self._chat_id):
            message_id = row["message_id"]
            try:
                pending = self._pending_from_row(row)
                tab = self._pick_invoice_tab(self._list_tabs(), pending.scope)
                supplier = SupplierMatch()
                if pending.fields.ice_fournisseur:
                    supplier = self.resolve_supplier(
                        pending.fields.ice_fournisseur, pending.fields.fournisseur
                    )
                message = self.import_invoice(pending, supplier=supplier, tab=tab)
            except (GmailWatcherError, InvoicePdfError, ValueError) as exc:
                logger.warning("Reprise d'import impossible (message=%s): %s", message_id, exc)
                continue
            set_gmail_message_status(self._db_path, message_id, "confirmed")
            logger.info("Import repris et termine (message=%s)", message_id)
            outcomes.append(
                WatchOutcome(
                    pending=pending,
                    decision=Decision(action=ACTION_AUTO),
                    message=message,
                    needs_buttons=False,
                )
            )
        return outcomes

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
        if not fields.is_invoice:
            # La requete Gmail ne filtre plus sur un marqueur de sujet : c'est
            # le CONTENU du PDF qui decide. Un devis, un bon de livraison ou
            # une plaquette n'atteint jamais le classeur.
            raise GmailWatcherError("le PDF n'est pas une facture")
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
                "ice_fournisseur": pending.fields.ice_fournisseur,
                "ice_client": pending.fields.ice_client,
                "lignes": [
                    {
                        "description": l.description,
                        "quantite": str(l.quantite),
                        "prix_unitaire_ht": str(l.prix_unitaire_ht),
                        "taux_tva": str(l.taux_tva) if l.taux_tva is not None else None,
                        "total_ht": str(l.total_ht),
                    }
                    for l in pending.fields.lignes
                ],
                "missing": pending.fields.missing,
                "anomalies": pending.fields.anomalies,
                "ambigus": pending.fields.ambigus,
                "is_avoir": pending.fields.is_avoir,
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

    # -- resolution du fournisseur (par ICE) -------------------------------

    def _read_range(self, a1_range: str) -> list[list[Any]]:
        data = self._execute(
            "GOOGLESHEETS_BATCH_GET",
            {
                "spreadsheet_id": self._spreadsheet_id,
                "ranges": [a1_range],
                "valueRenderOption": "UNFORMATTED_VALUE",
            },
        )
        ranges = data.get("valueRanges") or []
        if not ranges:
            return []
        return ranges[0].get("values") or []

    def resolve_supplier(self, ice: str | None, name: str | None) -> SupplierMatch:
        """Cherche le fournisseur PAR ICE dans 03_FOURNISSEURS.

        L'ICE est l'identifiant fiscal unique : c'est la seule cle fiable.
        Le nom ne sert qu'a detecter une ambiguite (meme raison sociale sous
        un autre ICE), jamais a decider tout seul.
        """
        rows = self._read_range(f"{SUPPLIERS_TAB}!A2:C200")
        wanted_ice = (ice or "").strip()
        wanted_name = _norm_name(name)
        by_ice = [r for r in rows if len(r) > 2 and str(r[2]).strip() == wanted_ice and wanted_ice]
        if len(by_ice) == 1:
            return SupplierMatch(
                supplier_id=str(by_ice[0][0]).strip(),
                name=str(by_ice[0][1]).strip(),
                existing=True,
            )
        if len(by_ice) > 1:
            return SupplierMatch(ambiguous=True, reason="plusieurs fournisseurs avec le meme ICE")
        by_name = [
            r for r in rows
            if wanted_name and len(r) > 1 and _norm_name(str(r[1])) == wanted_name
        ]
        if by_name:
            # Meme raison sociale mais ICE different : on ne tranche pas.
            return SupplierMatch(
                ambiguous=True,
                reason="un fournisseur porte deja ce nom avec un autre ICE",
            )
        existing_ids = [str(r[0]).strip() for r in rows if r]
        return SupplierMatch(supplier_id=next_supplier_id(existing_ids), name=(name or "").strip())

    def create_supplier(self, match: SupplierMatch, ice: str, delai_paiement: int = 30) -> str:
        """Cree le fournisseur dans 03_FOURNISSEURS. Appele uniquement quand
        les donnees sont certaines (ICE present, nom lisible)."""
        rows = self._read_range(f"{SUPPLIERS_TAB}!A2:C200")
        row_index = len(rows) + 2
        self._execute(
            "GOOGLESHEETS_VALUES_UPDATE",
            {
                "spreadsheet_id": self._spreadsheet_id,
                "range": f"{SUPPLIERS_TAB}!A{row_index}:G{row_index}",
                "value_input_option": "RAW",
                "values": [[match.supplier_id, match.name, ice, "", "", "", delai_paiement]],
            },
        )
        logger.info("Fournisseur %s cree dans %s (ICE present)", match.supplier_id, SUPPLIERS_TAB)
        return match.supplier_id

    # -- doublons ----------------------------------------------------------

    def duplicate_state(self, fields: ExtractedInvoice, supplier_id: str, tab: str) -> DuplicateState:
        """Doublon garanti par (ICE fournisseur + numero de facture).

        Deux sources sont consultees : la base locale (empreintes) et le
        classeur lui-meme, pour rester juste meme apres une saisie manuelle.
        """
        state = DuplicateState()
        key = fingerprint(fields.ice_fournisseur, fields.numero)
        known = get_invoice_fingerprint(self._db_path, key) if key else None
        if known:
            state.certain = True
            state.existing_ref = str(known.get("stable_id") or "")
            return state

        numero = (fields.numero or "").strip().upper()
        if not numero:
            return state
        rows = self._read_range(f"{tab}!A2:D200")
        for row in rows:
            if len(row) < 3 or str(row[2]).strip().upper() != numero:
                continue
            existing_supplier = str(row[3]).strip() if len(row) > 3 else ""
            if supplier_id and existing_supplier == supplier_id:
                state.certain = True
            else:
                state.uncertain = True
            state.existing_ref = str(row[0]).strip()
            break
        return state

    # -- import automatique ------------------------------------------------

    def handle(self, pending: PendingInvoice) -> WatchOutcome:
        """Applique la politique d'import a une facture detectee.

        Import automatique si la facture est lisible, complete, coherente et
        non ambigue. Sinon, validation humaine. Un doublon certain n'est
        jamais ecrit.
        """
        fields = pending.fields
        tab = self._pick_invoice_tab(self._list_tabs(), pending.scope)

        supplier = SupplierMatch()
        if fields.ice_fournisseur:
            supplier = self.resolve_supplier(fields.ice_fournisseur, fields.fournisseur)
        duplicates = self.duplicate_state(fields, supplier.supplier_id, tab)
        decision = decide_invoice(
            fields, duplicates=duplicates, supplier_ambiguous=supplier.ambiguous
        )

        if decision.action == ACTION_DUPLICATE:
            set_gmail_message_status(self._db_path, pending.message_id, "duplicate")
            ref = f" (deja enregistree sous {decision.existing_ref})" if decision.existing_ref else ""
            return WatchOutcome(
                pending=pending,
                decision=decision,
                message=(
                    f"Facture {fields.numero} deja importee{ref}. "
                    "Rien n'a ete ecrit : aucun doublon n'est cree."
                ),
                needs_buttons=False,
            )

        if decision.action == ACTION_REVIEW:
            reasons = "\n".join(f"- {r}" for r in decision.reasons)
            return WatchOutcome(
                pending=pending,
                decision=decision,
                message=(
                    build_preview(pending)
                    + "\n\nValidation humaine demandee car :\n"
                    + reasons
                ),
                needs_buttons=True,
            )

        try:
            result = self.import_invoice(pending, supplier=supplier, tab=tab)
        except PartialImportError as exc:
            # La comptabilite est juste ; il ne manque que l'archivage. On
            # informe sans proposer de bouton : rien n'est a decider.
            return WatchOutcome(
                pending=pending,
                decision=decision,
                message=(
                    f"Facture {fields.numero} enregistree dans le classeur.\n{exc}"
                ),
                needs_buttons=False,
            )
        set_gmail_message_status(self._db_path, pending.message_id, "confirmed")
        return WatchOutcome(
            pending=pending, decision=decision, message=result, needs_buttons=False
        )

    def import_invoice(
        self, pending: PendingInvoice, *, supplier: SupplierMatch, tab: str
    ) -> str:
        """Ecrit reellement la facture. Ne doit etre appelee qu'apres une
        decision d'import (automatique) ou une confirmation humaine."""
        if not self._spreadsheet_id:
            raise GmailWatcherError("GOOGLE_SHEET_ID manquant : ecriture impossible.")
        fields = pending.fields
        key = fingerprint(fields.ice_fournisseur, fields.numero)
        state = get_invoice_fingerprint(self._db_path, key) if key else None
        resuming = bool(state and state.get("stable_id"))
        if not resuming and not claim_invoice_fingerprint(
            self._db_path, key,
            numero=fields.numero or "", ice=fields.ice_fournisseur or "",
            message_id=pending.message_id,
        ):
            raise GmailWatcherError("Facture deja importee (empreinte ICE + numero).")

        # --- etape 1 : la ligne comptable ---------------------------------
        # Si elle a deja ete ecrite lors d'une tentative precedente, on la
        # reprend telle quelle : jamais de seconde ligne pour la meme facture.
        if resuming:
            plan = self._plan_from_state(state, pending, supplier)
            logger.info(
                "Reprise de l'import de %s (%s ligne %s deja ecrite)",
                fields.numero, plan.tab, plan.row_index,
            )
        else:
            try:
                if not supplier.existing and supplier.supplier_id and fields.ice_fournisseur:
                    self.create_supplier(supplier, fields.ice_fournisseur)

                ids = [str(r[0]).strip() for r in self._read_range(f"{tab}!A2:A200") if r]
                row_index = len(ids) + 2
                plan = build_row_plan(
                    tab=tab,
                    row_index=row_index,
                    stable_id=next_stable_invoice_id(ids, fields.date_facture.year),
                    supplier_id=supplier.supplier_id,
                    supplier_name=supplier.name or (fields.fournisseur or ""),
                    numero=fields.numero or "",
                    description=f"Import email - {pending.attachment_name}".strip(" -"),
                    date_facture=fields.date_facture,
                    date_echeance=fields.date_echeance,
                    montant_ht=fields.montant_ht,
                    taux_tva=fields.taux_tva,
                    montant_tva=fields.montant_tva,
                    montant_ttc=fields.montant_ttc,
                    statut=fields.statut,
                )
                self.write_row_plan(plan)
            except Exception:
                # Rien n'a ete ecrit en comptabilite : on libere l'empreinte
                # pour que la facture puisse etre retentee normalement.
                release_invoice_fingerprint(self._db_path, key)
                raise
            # Point de reprise : a partir d'ici la ligne EXISTE dans le
            # classeur, l'empreinte ne doit plus jamais etre liberee.
            update_invoice_fingerprint(
                self._db_path, key,
                stable_id=plan.stable_id, tab=plan.tab, row_index=plan.row_index,
            )
            set_gmail_message_status(self._db_path, pending.message_id, "partial")

        state = get_invoice_fingerprint(self._db_path, key) or {}

        # --- etape 2 : les lignes de detail -------------------------------
        if not state.get("lines_written"):
            self.write_detail_lines(plan.stable_id, plan.tab, fields)
            update_invoice_fingerprint(self._db_path, key, lines_written=1)

        # --- etape 3 : l'archivage Drive ----------------------------------
        drive_link = str(state.get("drive_link") or "")
        if not drive_link:
            try:
                drive_link = self._archive_to_drive(
                    pending.message_id, pending.attachment_name or "facture.pdf"
                )
                update_invoice_fingerprint(self._db_path, key, drive_link=drive_link)
            except GmailWatcherError as exc:
                # L'ecriture comptable reste valide : on laisse la facture en
                # 'partial' pour que le cycle suivant termine l'archivage.
                logger.warning(
                    "Archivage Drive impossible (message=%s): %s", pending.message_id, exc
                )
                set_gmail_message_status(self._db_path, pending.message_id, "partial")
                raise PartialImportError(
                    f"Facture ecrite dans {plan.tab} ligne {plan.row_index}, "
                    "mais archivage Drive incomplet : il sera termine au prochain cycle."
                ) from exc

        # --- etape 4 : le journal d'import --------------------------------
        if not state.get("log_row"):
            log_row = self.append_import_log(
                plan, pending, fields, supplier, drive_link=drive_link, action="Créé"
            )
            update_invoice_fingerprint(self._db_path, key, log_row=log_row)

        logger.info(
            "Facture %s importee dans %s ligne %d (Drive: %s)",
            fields.numero, plan.tab, plan.row_index, "oui" if drive_link else "non",
        )
        return (
            "Facture importée avec succès\n"
            f"- Numéro      : {fields.numero}\n"
            f"- Fournisseur : {supplier.name or fields.fournisseur}\n"
            f"- HT          : {fields.montant_ht} MAD\n"
            f"- TVA         : {fields.montant_tva} MAD\n"
            f"- TTC         : {fields.montant_ttc} MAD\n"
            f"- Onglet      : {plan.tab}\n"
            f"- Ligne       : {plan.row_index}"
        )

    def _plan_from_state(self, state: dict[str, Any], pending: PendingInvoice,
                         supplier: SupplierMatch):
        """Reconstruit le plan d'une ligne DEJA ecrite, a partir des points de
        reprise. Aucune cellule n'est reecrite : ce plan ne sert qu'a nommer
        l'onglet, la ligne et l'ID stable pour terminer les etapes suivantes."""
        fields = pending.fields
        return build_row_plan(
            tab=str(state.get("tab") or ""),
            row_index=int(state.get("row_index") or 2),
            stable_id=str(state.get("stable_id") or ""),
            supplier_id=supplier.supplier_id,
            supplier_name=supplier.name or (fields.fournisseur or ""),
            numero=fields.numero or "",
            description=f"Import email - {pending.attachment_name}".strip(" -"),
            date_facture=fields.date_facture,
            date_echeance=fields.date_echeance,
            montant_ht=fields.montant_ht,
            taux_tva=fields.taux_tva,
            montant_tva=fields.montant_tva,
            montant_ttc=fields.montant_ttc,
            statut=fields.statut,
        )

    # -- ecritures elementaires -------------------------------------------

    def write_row_plan(self, plan) -> None:
        """Ecrit la ligne : valeurs natives en RAW, formules en USER_ENTERED,
        puis formats et validation recopies du modele du classeur."""
        for a1_range, values, option in (
            (plan.range_a_j, plan.values_a_j, "RAW"),
            (plan.range_n_p, plan.values_n_p, "RAW"),
            (plan.range_k_m, plan.formulas_k_m, "USER_ENTERED"),
            (plan.range_q, [plan.formula_q], "USER_ENTERED"),
        ):
            self._execute(
                "GOOGLESHEETS_VALUES_UPDATE",
                {
                    "spreadsheet_id": self._spreadsheet_id,
                    "range": a1_range,
                    "value_input_option": option,
                    "values": [values],
                },
            )
        self.apply_row_formats(plan.tab, plan.row_index)

    def apply_row_formats(self, tab: str, row_index: int) -> None:
        """Reapplique les formats reels du classeur (MAD, %, dates) et la
        liste de validation du statut."""
        background = self._row_background(tab, row_index)
        specs = [
            (MONEY_COLUMNS, "CURRENCY", MONEY_PATTERN),
            (DATE_COLUMNS, "DATE", DATE_PATTERN),
            ((RATE_COLUMN,), "NUMBER", RATE_PATTERN),
        ]
        for columns, kind, pattern in specs:
            for column in columns:
                try:
                    self._execute(
                        "GOOGLESHEETS_FORMAT_CELL",
                        {
                            "spreadsheet_id": self._spreadsheet_id,
                            "sheet_name": tab,
                            "range": f"{column}{row_index}",
                            "number_format_type": kind,
                            "number_format_pattern": pattern,
                            "background_color": background,
                        },
                    )
                except GmailWatcherError as exc:
                    logger.warning("Format %s%d non applique: %s", column, row_index, exc)
        try:
            self._execute(
                "GOOGLESHEETS_SET_DATA_VALIDATION_RULE",
                {
                    "spreadsheet_id": self._spreadsheet_id,
                    "sheet_id": self._sheet_id(tab),
                    "mode": "SET",
                    "validation_type": "ONE_OF_LIST",
                    "values": list(STATUS_VALUES),
                    "strict": True,
                    "show_custom_ui": True,
                    "start_row_index": row_index - 1,
                    "end_row_index": row_index,
                    "start_column_index": 15,
                    "end_column_index": 16,
                },
            )
        except GmailWatcherError as exc:
            logger.warning("Validation du statut non appliquee ligne %d: %s", row_index, exc)

    def _row_background(self, tab: str, row_index: int) -> str:
        """Couleur de fond REELLE de la ligne, pour ne jamais la repeindre en
        appliquant un format de nombre."""
        try:
            data = self._execute(
                "GOOGLESHEETS_GET_SPREADSHEET_INFO",
                {
                    "spreadsheet_id": self._spreadsheet_id,
                    "ranges": [f"{tab}!A{row_index}:A{row_index}"],
                    "fields": "sheets.data.rowData.values(effectiveFormat.backgroundColor)",
                },
            )
            cell = data["sheets"][0]["data"][0]["rowData"][0]["values"][0]
            color = cell["effectiveFormat"]["backgroundColor"]
        except Exception:  # noqa: BLE001 - format inconnu : on reste en blanc
            return DEFAULT_ROW_BACKGROUND
        return "#" + "".join(
            f"{round(float(color.get(channel, 1)) * 255):02x}"
            for channel in ("red", "green", "blue")
        )

    def _sheet_id(self, tab: str) -> int:
        data = self._execute(
            "GOOGLESHEETS_GET_SPREADSHEET_INFO", {"spreadsheet_id": self._spreadsheet_id}
        )
        for sheet in data.get("sheets", []):
            props = sheet.get("properties") or {}
            if props.get("title") == tab:
                return int(props.get("sheetId", 0))
        raise GmailWatcherError(f"Onglet '{tab}' introuvable.")

    def write_detail_lines(self, stable_id: str, tab: str, fields: ExtractedInvoice) -> int:
        """Conserve les lignes detaillees dans un onglet dedie.

        Les onglets factures n'ont pas de colonnes pour le detail : on ajoute
        proprement 16_LIGNES_FACTURES, lie a l'ID de facture, sans toucher aux
        formules ni au tableau de bord.
        """
        if not fields.lignes:
            return 0
        rows = build_line_rows(
            stable_id=stable_id, tab=tab, numero=fields.numero or "", lignes=fields.lignes
        )
        self._ensure_lines_tab()
        existing = self._read_range(f"{LIGNES_TAB}!A2:A2000")
        start = len(existing) + 2
        end = start + len(rows) - 1
        self._execute(
            "GOOGLESHEETS_VALUES_UPDATE",
            {
                "spreadsheet_id": self._spreadsheet_id,
                "range": f"{LIGNES_TAB}!A{start}:I{end}",
                "value_input_option": "RAW",
                "values": rows,
            },
        )
        return len(rows)

    def _ensure_lines_tab(self) -> None:
        if LIGNES_TAB in self._list_tabs():
            return
        self._execute(
            "GOOGLESHEETS_ADD_SHEET",
            {
                "spreadsheet_id": self._spreadsheet_id,
                "title": LIGNES_TAB,
                "force_unique": False,
            },
        )
        self._execute(
            "GOOGLESHEETS_VALUES_UPDATE",
            {
                "spreadsheet_id": self._spreadsheet_id,
                "range": f"{LIGNES_TAB}!A1:I1",
                "value_input_option": "RAW",
                "values": [LIGNES_HEADERS],
            },
        )
        logger.info("Onglet %s cree (lignes de detail des factures)", LIGNES_TAB)

    def append_import_log(
        self, plan, pending: PendingInvoice, fields: ExtractedInvoice,
        supplier: SupplierMatch, *, drive_link: str = "", action: str = "Créé",
    ) -> int:
        from datetime import datetime, timezone

        row = build_import_log_row(
            horodatage=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            stable_id=plan.stable_id,
            action=action,
            statut="Importée automatiquement",
            numero=fields.numero or "",
            fournisseur=supplier.name or (fields.fournisseur or ""),
            ice=fields.ice_fournisseur or "",
            montant_ht=fields.montant_ht,
            montant_tva=fields.montant_tva,
            montant_ttc=fields.montant_ttc,
            tab=plan.tab,
            row_index=plan.row_index,
            gmail_message_id=pending.message_id,
            gmail_expediteur=pending.sender,
            gmail_objet=pending.subject,
            piece_jointe=pending.attachment_name,
            drive_lien=drive_link,
            type_enregistrement=(
                "Facture achat" if plan.tab.upper().find("ACHAT") >= 0 else "Facture vente"
            ),
        )
        existing = self._read_range(f"{IMPORTS_LOG_TAB}!A2:A2000")
        index = len(existing) + 2
        self._execute(
            "GOOGLESHEETS_VALUES_UPDATE",
            {
                "spreadsheet_id": self._spreadsheet_id,
                "range": f"{IMPORTS_LOG_TAB}!A{index}:F{index}",
                "value_input_option": "RAW",
                "values": [row],
            },
        )
        return index

    # -- apres validation humaine ------------------------------------------

    def confirm(self, message_id: str) -> str:
        """Ecrit la facture apres validation humaine (cas douteux uniquement)."""
        row = get_gmail_message(self._db_path, message_id)
        if row is None:
            raise GmailWatcherError("Facture introuvable (message inconnu).")
        if row["status"] == "confirmed":
            return "Cette facture a deja ete enregistree. Rien n'a ete duplique."
        if row["status"] == "refused":
            return "Cette facture avait ete refusee. Rien n'a ete ecrit."
        pending = self._pending_from_row(row)
        tab = self._pick_invoice_tab(self._list_tabs(), pending.scope)
        supplier = SupplierMatch()
        if pending.fields.ice_fournisseur:
            supplier = self.resolve_supplier(
                pending.fields.ice_fournisseur, pending.fields.fournisseur
            )
        if not supplier.supplier_id:
            existing = [str(r[0]).strip() for r in self._read_range(f"{SUPPLIERS_TAB}!A2:C200") if r]
            supplier = SupplierMatch(
                supplier_id=next_supplier_id(existing), name=pending.fields.fournisseur or ""
            )
        message = self.import_invoice(pending, supplier=supplier, tab=tab)
        set_gmail_message_status(self._db_path, message_id, "confirmed")
        return message

    def _pending_from_row(self, row: dict[str, Any]) -> PendingInvoice:
        from datetime import date as _date

        data = json.loads(row["payload"] or "{}")

        def as_decimal(key: str) -> Decimal | None:
            value = data.get(key)
            return Decimal(value) if value is not None else None

        def as_date(key: str) -> _date | None:
            value = data.get(key)
            return _date.fromisoformat(value) if value else None

        fields = ExtractedInvoice(
            numero=data.get("numero"),
            date_facture=as_date("date_facture"),
            date_echeance=as_date("date_echeance"),
            fournisseur=data.get("fournisseur"),
            client=data.get("client"),
            montant_ht=as_decimal("montant_ht"),
            taux_tva=as_decimal("taux_tva"),
            montant_tva=as_decimal("montant_tva"),
            montant_ttc=as_decimal("montant_ttc"),
            devise=data.get("devise") or "",
            statut=data.get("statut"),
            mode_paiement=data.get("mode_paiement"),
            ice_fournisseur=data.get("ice_fournisseur"),
            ice_client=data.get("ice_client"),
            missing=list(data.get("missing") or []),
            anomalies=list(data.get("anomalies") or []),
            ambigus=list(data.get("ambigus") or []),
            is_avoir=bool(data.get("is_avoir")),
        )
        fields.lignes = [
            InvoiceLine(
                description=l.get("description", ""),
                quantite=Decimal(l["quantite"]),
                prix_unitaire_ht=Decimal(l["prix_unitaire_ht"]),
                taux_tva=Decimal(l["taux_tva"]) if l.get("taux_tva") is not None else None,
                total_ht=Decimal(l["total_ht"]),
            )
            for l in (data.get("lignes") or [])
        ]
        return PendingInvoice(
            message_id=row["message_id"],
            thread_id=row["thread_id"] or "",
            subject=row["subject"] or "",
            sender=row["sender"] or "",
            received_at=row["received_at"] or "",
            attachment_name=row["attachment_name"] or "",
            fields=fields,
            scope=data.get("scope", "purchases"),
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
        """Archive le PDF et retourne son lien Drive (conserve dans le journal)."""
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
        uploaded = self._execute("GOOGLEDRIVE_UPLOAD_FROM_URL", args)
        return _drive_link(uploaded) or self._drive_folder

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


def _drive_link(uploaded: dict[str, Any]) -> str:
    """Lien consultable du fichier archive, quelle que soit la forme de la
    reponse Drive."""
    candidates = [uploaded, uploaded.get("file") or {}, uploaded.get("response_data") or {}]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("webViewLink", "webContentLink", "display_url", "link"):
            value = candidate.get(key)
            if value:
                return str(value)
        file_id = candidate.get("id")
        if file_id:
            return f"https://drive.google.com/file/d/{file_id}/view"
    return ""
