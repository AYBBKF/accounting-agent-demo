"""Pipeline documentaire : d'une piece jointe a une ligne comptable.

Chaque document traverse la meme sequence, et CHAQUE etape reussie est
persistee avant de passer a la suivante :

    detected -> downloaded -> extracted -> validated -> sheet_written
             -> details_written -> drive_archived -> [calendar_created]
             -> logged -> completed

Si une etape echoue apres l'ecriture comptable, le document reste en
`partial` : le cycle suivant reprend EXACTEMENT a l'etape manquante et ne
reecrit jamais la ligne Sheets.

Tous les appels Composio passent par la passerelle injectee, qui porte
l'isolation par `user_id = telegram_<chat_id>`. Aucun secret ne transite
par ce module.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from app import doc_store as store
from app.attachments import DocumentFile, idempotency_key
from app.doc_extract import ExtractedDocument, extract_from_pdf_bytes
from app.doc_policy import (
    ACTION_AUTO,
    ACTION_DUPLICATE,
    ACTION_REVIEW,
    ACTION_UNKNOWN,
    Decision,
    DecisionContext,
    DuplicateState,
    decide,
)
from app.doc_routing import (
    CUSTOMS_SPEC,
    NEW_TAB_SPECS,
    TAB_BANK,
    TAB_CLIENTS,
    TAB_CREDIT_NOTES,
    TAB_CUSTOMS,
    TAB_IMPORTS_LOG,
    TAB_INVOICE_LINES,
    TAB_PAYABLES,
    TAB_PURCHASES,
    TAB_SALES,
    TAB_SUPPLIERS,
    CALENDAR_EVENT_COLUMN,
    DRIVE_LINK_COLUMN,
    bank_line_fingerprint,
    build_bank_rows,
    build_commercial_row,
    build_credit_note_row,
    build_customs_row,
    build_payable_row,
    is_purchase_side,
    route_for,
)
from app.doc_types import (
    BANK_STATEMENT,
    CLIENT_CREDIT_NOTE,
    EXPORT_INVOICE,
    IMPORT_INVOICE,
    LABELS,
    PAYMENT_RECEIPT,
    PENALTY_NOTICE,
    PURCHASE_INVOICE,
    SALES_INVOICE,
    SUPPLIER_CREDIT_NOTE,
    UNKNOWN,
    normalize,
)
from app.invoice_sheet import (
    DATE_COLUMNS,
    DATE_PATTERN,
    LIGNES_HEADERS,
    LIGNES_TAB,
    MONEY_COLUMNS,
    MONEY_PATTERN,
    RATE_COLUMN,
    RATE_PATTERN,
    STATUS_VALUES,
    build_import_log_row,
    build_row_plan,
    next_stable_invoice_id,
    next_supplier_id,
    to_number,
    to_serial,
)

logger = logging.getLogger("demo_bot.doc_pipeline")

# Heure et fuseau des rappels d'echeance. Une echeance est une DATE ; le
# calendrier veut un instant. On pose donc le rappel en debut de matinee
# ouvrable plutot que d'inventer une heure au milieu de la nuit UTC.
REMINDER_HOUR = "09:00:00"
REMINDER_TIMEZONE = "Africa/Casablanca"

# Saut de ligne ecrit sans sequence d'echappement : le transport JSON des
# outils de publication reinterprete les sequences d'echappement et
# corromprait le fichier. Un module sans antislash traverse la chaine intact.
NEWLINE = chr(10)


class PipelineError(RuntimeError):
    """Erreur destinee aux logs et au client, jamais porteuse de secret."""


class PartialError(PipelineError):
    """La ligne comptable est ecrite ; une etape posterieure a echoue."""


class Gateway(Protocol):
    """Acces aux outils Composio, deja isole par client."""

    def execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class PartyMatch:
    party_id: str = ""
    name: str = ""
    existing: bool = False
    ambiguous: bool = False
    reason: str = ""


@dataclass
class DocumentOutcome:
    """Resultat du traitement d'UN document."""

    doc_key: str
    filename: str
    doc_type: str = UNKNOWN
    numero: str | None = None
    action: str = ACTION_REVIEW
    reasons: list[str] = field(default_factory=list)
    tab: str = ""
    row_index: int = 0
    stable_id: str = ""
    drive_link: str = ""
    calendar_event: str = ""
    tiers: str = ""
    montant_ht: Decimal | None = None
    montant_tva: Decimal | None = None
    montant_ttc: Decimal | None = None
    devise: str = ""
    echeance: date | None = None
    accounting: bool = False
    error: str = ""
    document: ExtractedDocument | None = None

    @property
    def type_label(self) -> str:
        return LABELS.get(self.doc_type, self.doc_type)


class DocumentPipeline:
    """Traite un document de bout en bout, avec reprise a l'etape exacte."""

    def __init__(
        self,
        gateway: Gateway,
        *,
        db_path: str,
        chat_id: int,
        spreadsheet_id: str,
        company: str = "X BLASTE",
        drive_root: str = "XBLASTE - Factures",
    ) -> None:
        self._gw = gateway
        self._db = db_path
        self._chat_id = chat_id
        self._sheet = spreadsheet_id
        self._company = company
        self._drive_root = drive_root
        self._tabs_cache: list[str] | None = None
        self._folder_cache: dict[str, str] = {}

    # -- utilitaires Sheets ------------------------------------------------

    def _read(self, a1_range: str) -> list[list[Any]]:
        data = self._gw.execute(
            "GOOGLESHEETS_BATCH_GET",
            {
                "spreadsheet_id": self._sheet,
                "ranges": [a1_range],
                "valueRenderOption": "UNFORMATTED_VALUE",
            },
        )
        ranges = data.get("valueRanges") or []
        return (ranges[0].get("values") or []) if ranges else []

    def _write(self, a1_range: str, values: list[list[Any]], *, raw: bool = True) -> None:
        self._gw.execute(
            "GOOGLESHEETS_VALUES_UPDATE",
            {
                "spreadsheet_id": self._sheet,
                "range": a1_range,
                "value_input_option": "RAW" if raw else "USER_ENTERED",
                "values": values,
            },
        )

    def tabs(self, *, refresh: bool = False) -> list[str]:
        if self._tabs_cache is None or refresh:
            data = self._gw.execute(
                "GOOGLESHEETS_GET_SPREADSHEET_INFO", {"spreadsheet_id": self._sheet}
            )
            self._tabs_cache = [
                t for t in (
                    (s.get("properties") or {}).get("title", "")
                    for s in data.get("sheets", [])
                ) if t
            ]
        return self._tabs_cache

    def sheet_id(self, tab: str) -> int:
        data = self._gw.execute(
            "GOOGLESHEETS_GET_SPREADSHEET_INFO", {"spreadsheet_id": self._sheet}
        )
        for sheet in data.get("sheets", []):
            props = sheet.get("properties") or {}
            if props.get("title") == tab:
                return int(props.get("sheetId", 0))
        raise PipelineError(f"Onglet '{tab}' introuvable.")

    def ensure_tab(self, tab: str) -> None:
        """Cree un onglet manquant DANS LE STYLE du classeur : en-tetes,
        formats de date et de devise, validations. Un onglet existant n'est
        jamais modifie."""
        if tab in self.tabs():
            return
        spec = NEW_TAB_SPECS.get(tab)
        if spec is None:
            raise PipelineError(f"Aucune specification pour l'onglet '{tab}'.")
        self._gw.execute(
            "GOOGLESHEETS_ADD_SHEET",
            {"spreadsheet_id": self._sheet, "title": tab, "force_unique": False},
        )
        self._write(f"{tab}!A1:{spec.last_column}1", [spec.headers])
        self._tabs_cache = None
        sheet_id = self.sheet_id(tab)
        for column in spec.date_columns:
            self._format_column(tab, column, "DATE", DATE_PATTERN)
        for column in spec.money_columns:
            self._format_column(tab, column, "CURRENCY", MONEY_PATTERN)
        for column in spec.rate_columns:
            self._format_column(tab, column, "NUMBER", RATE_PATTERN)
        for column, values in spec.validations.items():
            index = ord(column) - ord("A")
            try:
                self._gw.execute(
                    "GOOGLESHEETS_SET_DATA_VALIDATION_RULE",
                    {
                        "spreadsheet_id": self._sheet,
                        "sheet_id": sheet_id,
                        "mode": "SET",
                        "validation_type": "ONE_OF_LIST",
                        "values": list(values),
                        "strict": True,
                        "show_custom_ui": True,
                        "start_row_index": 1,
                        "end_row_index": 500,
                        "start_column_index": index,
                        "end_column_index": index + 1,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - validation non bloquante
                logger.warning("Validation %s!%s non appliquee: %s", tab, column, exc)
        logger.info("Onglet %s cree dans le style du classeur", tab)

    def _format_column(self, tab: str, column: str, kind: str, pattern: str) -> None:
        try:
            self._gw.execute(
                "GOOGLESHEETS_FORMAT_CELL",
                {
                    "spreadsheet_id": self._sheet,
                    "sheet_name": tab,
                    "range": f"{column}2:{column}500",
                    "number_format_type": kind,
                    "number_format_pattern": pattern,
                    "background_color": "#ffffff",
                },
            )
        except Exception as exc:  # noqa: BLE001 - format non bloquant
            logger.warning("Format %s!%s non applique: %s", tab, column, exc)

    def next_row(self, tab: str) -> int:
        return len(self._read(f"{tab}!A2:A2000")) + 2

    # -- tiers -------------------------------------------------------------

    def resolve_party(self, tab: str, ice: str | None, name: str | None) -> PartyMatch:
        """Cherche un tiers PAR ICE. Le nom ne sert qu'a detecter une
        ambiguite, jamais a decider seul."""
        rows = self._read(f"{tab}!A2:C200")
        wanted_ice = (ice or "").strip()
        wanted_name = normalize(name or "")
        by_ice = [r for r in rows if len(r) > 2 and wanted_ice and str(r[2]).strip() == wanted_ice]
        if len(by_ice) == 1:
            return PartyMatch(str(by_ice[0][0]).strip(), str(by_ice[0][1]).strip(), existing=True)
        if len(by_ice) > 1:
            return PartyMatch(ambiguous=True, reason="plusieurs tiers avec le meme ICE")
        by_name = [
            r for r in rows
            if wanted_name and len(r) > 1 and normalize(str(r[1])) == wanted_name
        ]
        if by_name:
            return PartyMatch(
                ambiguous=True, reason="un tiers porte deja ce nom avec un autre ICE"
            )
        prefix = "FRS" if tab == TAB_SUPPLIERS else "CLI"
        existing = [str(r[0]).strip() for r in rows if r]
        return PartyMatch(next_supplier_id(existing, prefix), (name or "").strip())

    def create_party(self, tab: str, match: PartyMatch, ice: str) -> str:
        row_index = self.next_row(tab)
        self._write(
            f"{tab}!A{row_index}:G{row_index}",
            [[match.party_id, match.name, ice, "", "", "", 30]],
        )
        logger.info("Tiers %s cree dans %s", match.party_id, tab)
        return match.party_id

    # -- Drive -------------------------------------------------------------

    def ensure_folder(self, name: str, parent: str = "") -> str:
        """Retrouve ou cree UN dossier, toujours dans son parent.

        La recherche est bornee au parent et au nom EXACT. Une recherche non
        bornee renverrait le premier dossier venu du Drive : les pieces
        seraient archivees dans un dossier arbitraire, ce qui est pire qu'un
        echec franc.
        """
        cache_key = f"{parent}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        folder_id = ""
        try:
            query: dict[str, Any] = {"name_exact": name, "page_size": 10}
            if parent:
                query["parent_folder_id"] = parent
            found = self._gw.execute("GOOGLEDRIVE_FIND_FOLDER", query)
            folder_id = first_folder_id(found)
        except Exception:  # noqa: BLE001 - dossier absent
            folder_id = ""
        if not folder_id:
            try:
                args: dict[str, Any] = {"name": name}
                if parent:
                    args["parent_id"] = parent
                created = self._gw.execute("GOOGLEDRIVE_CREATE_FOLDER", args)
                folder_id = first_folder_id(created)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Dossier Drive '%s' non cree: %s", name, exc)
                return ""
        self._folder_cache[cache_key] = folder_id
        return folder_id

    def archive(self, file: DocumentFile, folder: str, year: int, source_url: str) -> str:
        """Archive le PDF dans <racine>/<categorie>/<annee> et retourne son lien."""
        root = self.ensure_folder(self._drive_root)
        category = self.ensure_folder(folder, root)
        target = self.ensure_folder(str(year), category) or category or root
        args: dict[str, Any] = {
            "source_url": source_url,
            "name": file.filename,
            "mime_type": "application/pdf",
        }
        if target:
            args["parent_folder_id"] = target
        uploaded = self._gw.execute("GOOGLEDRIVE_UPLOAD_FROM_URL", args)
        return drive_link(uploaded)

    # -- Calendar ----------------------------------------------------------

    def create_reminder(
        self, *, key: str, title: str, due: date, description: str
    ) -> str:
        """Cree un rappel d'echeance, une seule fois. Rien n'est cree si la
        date est absente : un rappel sans date certaine n'a aucune valeur."""
        if not store.claim_calendar_event(self._db, self._chat_id, key):
            logger.info("Evenement Calendar deja cree pour %s", key)
            return ""
        try:
            created = self._gw.execute(
                "GOOGLECALENDAR_CREATE_EVENT",
                {
                    "summary": title,
                    "description": description,
                    # L'API exige une heure : une date seule ("2026-08-31") est
                    # refusee par le format ISO attendu. Le rappel est pose en
                    # debut de matinee, dans le fuseau de la societe.
                    "start_datetime": f"{due.isoformat()}T{REMINDER_HOUR}",
                    "timezone": REMINDER_TIMEZONE,
                    "event_duration_hour": 0,
                    "event_duration_minutes": 30,
                    "create_meeting_room": False,
                },
            )
        except Exception as exc:  # noqa: BLE001 - Calendar non bloquant
            logger.warning("Evenement Calendar non cree (%s): %s", key, exc)
            return ""
        event_id = str(
            created.get("id")
            or (created.get("response_data") or {}).get("id")
            or (created.get("event") or {}).get("id")
            or ""
        )
        store.record_calendar_event(self._db, key, event_id)
        return event_id


    # -- traitement d'un document -----------------------------------------

    def process_document(
        self,
        file: DocumentFile,
        message: dict[str, Any],
        *,
        attachment_id: str,
        source_url: str = "",
        forced: bool = False,
    ) -> DocumentOutcome:
        """Traite UN document de bout en bout, ou reprend la ou il en etait.

        Une erreur ici ne concerne que ce document : l'appelant continue avec
        les autres pieces jointes du meme email.
        """
        user_id = f"telegram_{self._chat_id}"
        message_id = str(message.get("messageId") or message.get("id") or "")
        doc_key = idempotency_key(user_id, message_id, attachment_id, file.sha256)
        outcome = DocumentOutcome(doc_key=doc_key, filename=file.display_name)

        existing = store.get_document(self._db, doc_key)
        if existing and existing["state"] in store.TERMINAL_STATES and not forced:
            outcome.action = ACTION_DUPLICATE
            outcome.doc_type = existing["doc_type"] or UNKNOWN
            outcome.numero = existing["numero"]
            outcome.stable_id = existing["stable_id"] or ""
            outcome.tab = existing["tab"] or ""
            outcome.row_index = int(existing["row_index"] or 0)
            outcome.reasons = ["document deja traite lors d'un cycle precedent"]
            return outcome
        resuming = bool(existing and existing["state"] in store.STATES_AFTER_SHEET)
        if not existing:
            store.claim_document(
                self._db, doc_key, self._chat_id,
                gmail_message_id=message_id, attachment_id=attachment_id,
                file_sha256=file.sha256, filename=file.filename,
                container=file.container,
            )
        store.set_state(self._db, doc_key, store.DOWNLOADED)

        # --- extraction ---------------------------------------------------
        try:
            doc = extract_from_pdf_bytes(file.content, company=self._company)
        except Exception as exc:  # noqa: BLE001 - PDF illisible
            store.set_state(self._db, doc_key, store.FAILED, error=str(exc))
            outcome.action = ACTION_REVIEW
            outcome.error = str(exc)
            outcome.reasons = [f"document illisible : {exc}"]
            return outcome

        outcome.document = doc
        outcome.doc_type = doc.doc_type
        outcome.numero = doc.numero
        outcome.devise = doc.devise
        outcome.echeance = doc.date_echeance
        outcome.montant_ht = doc.montant_ht.value if doc.montant_ht else None
        outcome.montant_tva = doc.montant_tva.value if doc.montant_tva else None
        outcome.montant_ttc = doc.montant_ttc.value if doc.montant_ttc else None
        route = route_for(doc.doc_type)
        outcome.accounting = route.accounting
        store.update_document(
            self._db, doc_key, state=store.EXTRACTED,
            doc_type=doc.doc_type, numero=doc.numero or "",
        )

        # --- doublons ------------------------------------------------------
        duplicates = DuplicateState()
        if not resuming:
            same_file = store.find_by_sha256(self._db, self._chat_id, file.sha256)
            if same_file and same_file["doc_key"] != doc_key:
                duplicates.certain = True
                duplicates.existing_ref = same_file["stable_id"] or same_file["doc_key"][:12]
            else:
                same_business = store.find_by_business_key(
                    self._db, self._chat_id, doc.doc_type, doc.numero or ""
                )
                if same_business and same_business["doc_key"] != doc_key:
                    duplicates.certain = True
                    duplicates.existing_ref = same_business["stable_id"] or ""

        # --- tiers ----------------------------------------------------------
        party = PartyMatch()
        party_tab = TAB_SUPPLIERS if is_purchase_side(doc.doc_type) else TAB_CLIENTS
        needs_party = doc.doc_type in (
            PURCHASE_INVOICE, SALES_INVOICE, IMPORT_INVOICE, EXPORT_INVOICE,
            SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE,
        )
        party_ice = (
            doc.emetteur_ice if is_purchase_side(doc.doc_type) else doc.destinataire_ice
        )
        party_name = doc.emetteur if is_purchase_side(doc.doc_type) else doc.destinataire
        if needs_party and party_ice:
            party = self.resolve_party(party_tab, party_ice, party_name)

        # --- rapprochement d'un recu ---------------------------------------
        receipt_matches: list[tuple[str, int, list[Any]]] = []
        if doc.doc_type == PAYMENT_RECEIPT:
            receipt_matches = self.find_invoice(doc)

        context = DecisionContext(
            duplicates=duplicates,
            party_ambiguous=party.ambiguous,
            party_reason=party.reason,
            receipt_matches=len(receipt_matches),
        )
        decision = decide(doc, context)
        if forced and decision.action == ACTION_REVIEW:
            # Le client a valide en connaissance de cause : on ecrit, en
            # conservant la trace des motifs qui avaient bloque l'import.
            logger.info(
                "Ecriture forcee apres validation humaine (%s) : %s",
                outcome.filename, "; ".join(decision.reasons),
            )
            decision = Decision(action=ACTION_AUTO, reasons=decision.reasons)
        outcome.action = decision.action
        outcome.reasons = list(decision.reasons)
        outcome.tiers = party.name or party_name or ""

        if decision.action == ACTION_DUPLICATE:
            # Meme validee, une facture deja enregistree n'est jamais ecrite
            # deux fois : ce serait une double ecriture comptable.
            store.set_state(self._db, doc_key, store.DUPLICATE)
            outcome.stable_id = decision.existing_ref
            return outcome

        if decision.action == ACTION_REVIEW:
            store.update_document(
                self._db, doc_key, state=store.NEEDS_REVIEW,
                payload=json.dumps({"reasons": decision.reasons}, ensure_ascii=False),
            )
            return outcome

        if decision.action == ACTION_UNKNOWN:
            # Aucune ecriture : le document part dans la zone "A verifier".
            try:
                outcome.drive_link = self.archive(
                    file, route.drive_folder, (doc.date_document or date.today()).year,
                    source_url,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Archivage 'A verifier' impossible: %s", exc)
            store.update_document(
                self._db, doc_key, state=store.SKIPPED, drive_link=outcome.drive_link
            )
            self._safe_log(outcome, message)
            return outcome

        store.set_state(self._db, doc_key, store.VALIDATED)
        return self._write_document(
            outcome, doc, file, message, party, party_tab, route, receipt_matches, source_url,
            resuming=resuming, existing=existing or {}, forced=forced,
        )

    def _write_document(
        self, outcome, doc, file, message, party, party_tab, route, receipt_matches,
        source_url, *, resuming: bool, existing: dict[str, Any], forced: bool = False,
    ) -> DocumentOutcome:
        """Les ecritures proprement dites, chacune avec son point de reprise."""
        doc_key = outcome.doc_key

        # --- etape 1 : la ligne principale --------------------------------
        if resuming and existing.get("stable_id"):
            outcome.stable_id = existing["stable_id"]
            outcome.tab = existing["tab"] or ""
            outcome.row_index = int(existing["row_index"] or 0)
            logger.info(
                "Reprise de %s : %s ligne %s deja ecrite",
                outcome.filename, outcome.tab, outcome.row_index,
            )
        else:
            try:
                self._write_primary(
                    outcome, doc, party, party_tab, route, receipt_matches, forced=forced
                )
            except Exception as exc:  # noqa: BLE001
                store.release_document(self._db, doc_key)
                store.set_state(self._db, doc_key, store.FAILED, error=str(exc))
                outcome.action = ACTION_REVIEW
                outcome.error = str(exc)
                outcome.reasons = [f"ecriture impossible : {exc}"]
                return outcome
            store.update_document(
                self._db, doc_key, state=store.SHEET_WRITTEN,
                stable_id=outcome.stable_id, tab=outcome.tab, row_index=outcome.row_index,
            )

        state = store.get_document(self._db, doc_key) or {}

        # --- etape 2 : lignes de detail ------------------------------------
        if not state.get("lines_written"):
            if doc.doc_type in (PURCHASE_INVOICE, SALES_INVOICE, IMPORT_INVOICE, EXPORT_INVOICE):
                self.write_invoice_lines(doc, outcome.stable_id, outcome.tab)
            store.update_document(self._db, doc_key, lines_written=1, state=store.DETAILS_WRITTEN)

        # --- etape 3 : archivage Drive -------------------------------------
        outcome.drive_link = str(state.get("drive_link") or "")
        if not outcome.drive_link:
            try:
                outcome.drive_link = self.archive(
                    file, route.drive_folder,
                    (doc.date_document or date.today()).year, source_url,
                )
                store.update_document(
                    self._db, doc_key, drive_link=outcome.drive_link,
                    state=store.DRIVE_ARCHIVED,
                )
                self._backfill(outcome.tab, outcome.row_index,
                               DRIVE_LINK_COLUMN, outcome.drive_link)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Archivage Drive impossible (%s): %s", outcome.filename, exc)
                store.set_state(self._db, doc_key, store.PARTIAL)
                outcome.error = "archivage Drive a terminer au prochain cycle"
                return outcome

        # --- etape 4 : rappel Calendar -------------------------------------
        outcome.calendar_event = str(state.get("calendar_event") or "")
        if route.calendar and not outcome.calendar_event and doc.date_echeance:
            outcome.calendar_event = self.create_reminder(
                key=f"{self._chat_id}|{doc.doc_type}|{doc.numero}",
                title=f"{LABELS.get(doc.doc_type)} {doc.numero} - {outcome.montant_ttc} {doc.devise}",
                due=doc.date_echeance,
                description=(
                    f"Document {doc.numero} a payer avant le {doc.date_echeance}."
                    f"{NEWLINE}Piece archivee : {outcome.drive_link}"
                ),
            )
            store.update_document(
                self._db, doc_key, calendar_event=outcome.calendar_event,
                state=store.CALENDAR_CREATED,
            )
            self._backfill(outcome.tab, outcome.row_index,
                           CALENDAR_EVENT_COLUMN, outcome.calendar_event)

        # --- etape 5 : journal d'import ------------------------------------
        if not state.get("log_row"):
            log_row = self._safe_log(outcome, message)
            store.update_document(self._db, doc_key, log_row=log_row, state=store.LOGGED)

        store.set_state(self._db, doc_key, store.COMPLETED)
        return outcome

    def _write_primary(
        self, outcome, doc, party, party_tab, route, receipt_matches, *, forced: bool = False
    ) -> None:
        """Ecrit la ligne principale, selon le type de document."""
        kind = doc.doc_type
        year = (doc.date_document or date.today()).year

        if kind in (PURCHASE_INVOICE, SALES_INVOICE, IMPORT_INVOICE, EXPORT_INVOICE):
            if party.party_id and not party.existing:
                ice = doc.emetteur_ice if is_purchase_side(kind) else doc.destinataire_ice
                if ice or forced:
                    self.create_party(party_tab, party, ice or "")
            tab = TAB_PURCHASES if is_purchase_side(kind) else TAB_SALES
            outcome.stable_id, outcome.row_index = self.write_invoice(doc, party, tab)
            outcome.tab = tab
            if kind in (IMPORT_INVOICE, EXPORT_INVOICE):
                customs_id = (
                    self.next_prefixed_id(TAB_CUSTOMS, "DOU", year)
                    if TAB_CUSTOMS in self.tabs() else f"DOU-{year}-001"
                )
                self.append_row(
                    TAB_CUSTOMS,
                    build_customs_row(
                        stable_id=customs_id, invoice_id=outcome.stable_id, doc=doc,
                        freight=doc.frais_annexes.value if doc.frais_annexes else None,
                    ),
                    CUSTOMS_SPEC.last_column,
                )
            return

        if kind in (SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE):
            self.ensure_tab(TAB_CREDIT_NOTES)
            outcome.stable_id = self.next_prefixed_id(TAB_CREDIT_NOTES, "AV", year)
            outcome.row_index = self.append_row(
                TAB_CREDIT_NOTES,
                build_credit_note_row(
                    stable_id=outcome.stable_id, doc=doc, party_id=party.party_id
                ),
                "N",
            )
            outcome.tab = TAB_CREDIT_NOTES
            return

        if not route.accounting:
            from app.doc_routing import TAB_COMMERCIAL_DOCS

            self.ensure_tab(TAB_COMMERCIAL_DOCS)
            outcome.stable_id = self.next_prefixed_id(TAB_COMMERCIAL_DOCS, "DOC", year)
            outcome.row_index = self.append_row(
                TAB_COMMERCIAL_DOCS,
                build_commercial_row(stable_id=outcome.stable_id, doc=doc),
                "L",
            )
            outcome.tab = TAB_COMMERCIAL_DOCS
            return

        if kind == PENALTY_NOTICE:
            self.ensure_tab(TAB_PAYABLES)
            outcome.stable_id = self.next_prefixed_id(TAB_PAYABLES, "ECH", year)
            outcome.row_index = self.append_row(
                TAB_PAYABLES,
                build_payable_row(stable_id=outcome.stable_id, doc=doc, motif=doc.motif),
                "K",
            )
            outcome.tab = TAB_PAYABLES
            return

        if kind == BANK_STATEMENT:
            written, start = self.write_bank_statement(doc)
            outcome.tab = TAB_BANK
            outcome.row_index = start
            outcome.stable_id = doc.numero or f"REL-{year}"
            outcome.reasons.append(f"{written} operation(s) bancaire(s) ecrite(s)")
            return

        if kind == PAYMENT_RECEIPT:
            if len(receipt_matches) != 1:
                raise PipelineError(
                    "aucune facture unique ne correspond a ce recu : rien n'est solde"
                )
            tab, row_index, _ = receipt_matches[0]
            amount = doc.montant_paye.value if doc.montant_paye else Decimal("0")
            self.settle_invoice(tab, row_index, amount)
            outcome.tab = tab
            outcome.row_index = row_index
            outcome.stable_id = doc.numero or ""
            outcome.reasons.append(f"facture soldee dans {tab} ligne {row_index}")
            return

        raise PipelineError(f"Type '{kind}' sans regle d'ecriture.")

    def _backfill(self, tab: str, row_index: int, columns: dict[str, str], value: str) -> None:
        """Reporte une valeur (lien Drive, evenement Calendar) dans la ligne
        metier, uniquement si l'onglet possede reellement la colonne."""
        column = columns.get(tab)
        if not column or not row_index or not value:
            return
        try:
            self._write(f"{tab}!{column}{row_index}", [[value]])
        except Exception as exc:  # noqa: BLE001 - report non bloquant
            logger.warning("Report %s!%s%d impossible: %s", tab, column, row_index, exc)

    def _safe_log(self, outcome: DocumentOutcome, message: dict[str, Any]) -> int:
        try:
            return self.append_import_log(outcome, message)
        except Exception as exc:  # noqa: BLE001 - le journal ne bloque jamais
            logger.warning("Journal d'import non ecrit (%s): %s", outcome.filename, exc)
            return 0

    # -- rapprochement d'un recu ------------------------------------------

    def find_invoice(self, doc: ExtractedDocument) -> list[tuple[str, int, list[Any]]]:
        """Factures candidates pour un recu. Jamais sur le seul montant.

        La recherche se fait d'abord sur le numero de facture cite par le
        recu. A defaut, sur le couple (tiers + montant TTC) - et si plusieurs
        factures correspondent, aucune n'est soldee automatiquement.
        """
        wanted_number = normalize(doc.facture_liee or "")
        amount = doc.montant_paye.value if doc.montant_paye else None
        payer = normalize(doc.emetteur or "")
        matches: list[tuple[str, int, list[Any]]] = []
        for tab in (TAB_SALES, TAB_PURCHASES):
            for offset, row in enumerate(self._read(f"{tab}!A2:Q400")):
                if len(row) < 10:
                    continue
                number = normalize(str(row[2]))
                if wanted_number and number == wanted_number:
                    matches.append((tab, offset + 2, row))
                    continue
                if wanted_number:
                    continue
                party_name = normalize(str(row[4])) if len(row) > 4 else ""
                try:
                    ttc = Decimal(str(row[9]))
                except Exception:  # noqa: BLE001 - cellule non numerique
                    continue
                if amount is not None and ttc == amount and payer and party_name == payer:
                    matches.append((tab, offset + 2, row))
        return matches

    def settle_invoice(self, tab: str, row_index: int, amount: Decimal) -> None:
        """Solde une facture : montant paye et statut, rien d'autre."""
        self._write(f"{tab}!O{row_index}:P{row_index}", [[to_number(amount), "Payee"]])

    # -- ecritures par type ------------------------------------------------

    def write_invoice(
        self, doc: ExtractedDocument, party: PartyMatch, tab: str
    ) -> tuple[str, int]:
        ids = [str(r[0]).strip() for r in self._read(f"{tab}!A2:A400") if r]
        row_index = len(ids) + 2
        prefix = "FA" if tab == TAB_PURCHASES else "FV"
        stable_id = next_stable_invoice_id(ids, doc.date_document.year, prefix)
        plan = build_row_plan(
            tab=tab,
            row_index=row_index,
            stable_id=stable_id,
            supplier_id=party.party_id,
            supplier_name=party.name or (doc.emetteur or doc.destinataire or ""),
            numero=doc.numero or "",
            description=f"Import email - {doc.numero or ''}".strip(" -"),
            date_facture=doc.date_document,
            date_echeance=doc.date_echeance,
            montant_ht=doc.montant_ht.value if doc.montant_ht else Decimal("0"),
            taux_tva=doc.taux_tva,
            montant_tva=doc.montant_tva.value if doc.montant_tva else Decimal("0"),
            montant_ttc=doc.montant_ttc.value if doc.montant_ttc else Decimal("0"),
            statut=doc.statut,
        )
        for a1, values, raw in (
            (plan.range_a_j, plan.values_a_j, True),
            (plan.range_n_p, plan.values_n_p, True),
            (plan.range_k_m, plan.formulas_k_m, False),
            (plan.range_q, [plan.formula_q], False),
        ):
            self._write(a1, [values], raw=raw)
        self._apply_invoice_formats(tab, row_index)
        return stable_id, row_index

    def _apply_invoice_formats(self, tab: str, row_index: int) -> None:
        for columns, kind, pattern in (
            (MONEY_COLUMNS, "CURRENCY", MONEY_PATTERN),
            (DATE_COLUMNS, "DATE", DATE_PATTERN),
            ((RATE_COLUMN,), "NUMBER", RATE_PATTERN),
        ):
            for column in columns:
                try:
                    self._gw.execute(
                        "GOOGLESHEETS_FORMAT_CELL",
                        {
                            "spreadsheet_id": self._sheet,
                            "sheet_name": tab,
                            "range": f"{column}{row_index}",
                            "number_format_type": kind,
                            "number_format_pattern": pattern,
                            "background_color": "#ffffff",
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Format %s%d non applique: %s", column, row_index, exc)
        try:
            self._gw.execute(
                "GOOGLESHEETS_SET_DATA_VALIDATION_RULE",
                {
                    "spreadsheet_id": self._sheet,
                    "sheet_id": self.sheet_id(tab),
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("Validation du statut non appliquee ligne %d: %s", row_index, exc)

    def write_invoice_lines(self, doc: ExtractedDocument, stable_id: str, tab: str) -> int:
        if not doc.lignes:
            return 0
        rows = [
            [
                stable_id, tab, doc.numero or "", index,
                line.description,
                to_number(line.quantite) if line.quantite is not None else "",
                to_number(line.prix_unitaire) if line.prix_unitaire is not None else "",
                to_number(line.taux_tva) if line.taux_tva is not None else "",
                to_number(line.total) if line.total is not None else "",
            ]
            for index, line in enumerate(doc.lignes, start=1)
        ]
        if LIGNES_TAB not in self.tabs():
            self._gw.execute(
                "GOOGLESHEETS_ADD_SHEET",
                {"spreadsheet_id": self._sheet, "title": LIGNES_TAB, "force_unique": False},
            )
            self._write(f"{LIGNES_TAB}!A1:I1", [LIGNES_HEADERS])
            self._tabs_cache = None
        start = self.next_row(LIGNES_TAB)
        self._write(f"{LIGNES_TAB}!A{start}:I{start + len(rows) - 1}", rows)
        return len(rows)

    def write_bank_statement(self, doc: ExtractedDocument) -> tuple[int, int]:
        """Ecrit les operations nouvelles. Les operations deja connues sont
        ignorees grace a leur empreinte : deux releves qui se chevauchent ne
        creent pas de doublon."""
        account = doc.destinataire or "Banque Principale DEMO"
        fresh = [
            line for line in doc.bank_lines
            if store.claim_bank_line(self._db, self._chat_id,
                                     bank_line_fingerprint(account, line))
        ]
        if not fresh:
            return 0, 0
        subset = ExtractedDocument(classification=doc.classification)
        subset.bank_lines = fresh
        start = self.next_row(TAB_BANK)
        rows = build_bank_rows(start_index=start - 1, doc=subset)
        self._write(f"{TAB_BANK}!A{start}:M{start + len(rows) - 1}", rows)
        return len(rows), start

    def append_row(self, tab: str, values: list[Any], width: str) -> int:
        self.ensure_tab(tab)
        row_index = self.next_row(tab)
        self._write(f"{tab}!A{row_index}:{width}{row_index}", [values])
        return row_index

    def next_prefixed_id(self, tab: str, prefix: str, year: int) -> str:
        ids = [str(r[0]).strip() for r in self._read(f"{tab}!A2:A400") if r]
        return next_stable_invoice_id(ids, year, prefix)

    def append_import_log(self, outcome: DocumentOutcome, message: dict[str, Any]) -> int:
        from datetime import datetime, timezone

        doc = outcome.document
        row = build_import_log_row(
            horodatage=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            stable_id=outcome.stable_id or outcome.doc_key[:12],
            action="Cree",
            statut=f"{outcome.type_label} - {outcome.action}",
            numero=outcome.numero or "",
            fournisseur=outcome.tiers,
            ice=(doc.emetteur_ice or doc.destinataire_ice or "") if doc else "",
            montant_ht=outcome.montant_ht,
            montant_tva=outcome.montant_tva,
            montant_ttc=outcome.montant_ttc,
            tab=outcome.tab or "(aucun onglet)",
            row_index=outcome.row_index,
            gmail_message_id=str(message.get("messageId") or ""),
            gmail_expediteur=str(message.get("sender") or ""),
            gmail_objet=str(message.get("subject") or ""),
            piece_jointe=outcome.filename,
            drive_lien=outcome.drive_link,
            type_enregistrement=outcome.type_label,
        )
        index = self.next_row(TAB_IMPORTS_LOG)
        self._write(f"{TAB_IMPORTS_LOG}!A{index}:F{index}", [row])
        return index


def first_folder_id(payload: dict[str, Any]) -> str:
    """Identifiant du premier dossier d'une reponse Drive, quelle que soit sa forme."""
    if not isinstance(payload, dict):
        return ""
    for key in ("files", "folders", "items"):
        items = payload.get(key) or []
        if isinstance(items, list) and items and isinstance(items[0], dict):
            found = items[0].get("id")
            if found:
                return str(found)
    for candidate in (payload, payload.get("file") or {}, payload.get("folder") or {}):
        if isinstance(candidate, dict) and candidate.get("id"):
            return str(candidate["id"])
    return ""


def drive_link(uploaded: dict[str, Any]) -> str:
    """Lien consultable, quelle que soit la forme de la reponse Drive."""
    for candidate in (uploaded, uploaded.get("file") or {}, uploaded.get("response_data") or {}):
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
