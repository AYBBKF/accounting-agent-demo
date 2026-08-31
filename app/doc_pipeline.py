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

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Protocol

from app import doc_store as store
from app.attachments import DocumentFile, content_mimetype, idempotency_key, is_image
from app.business_key import business_identity
from app import doc_vision
from app import archive_log
from app import ledger
from app import llm_usage
from app.doc_extract import (
    ExtractedDocument,
    extract_from_image_bytes,
    extract_from_pdf_bytes,
)
from app.doc_policy import (
    NotWritable,
    assert_writable,
    ACTION_AUTO,
    ACTION_DUPLICATE,
    ACTION_REVIEW,
    ACTION_UNKNOWN,
    Decision,
    DecisionContext,
    DuplicateState,
    decide,
)
TAB_JOURNAL = "12_JOURNAL_COMPTABLE"
TAB_TVA_RECAP = "BOT_TVA_RECAP"

from app.doc_routing import (
    CUSTOMS_SPEC,
    NEW_TAB_SPECS,
    TAB_BANK,
    TAB_RECONCILIATION,
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
from app.review_sheet import (
    COL_ANOMALY,
    LAST_COL as REVIEW_LAST_COL,
    REVIEW_HEADERS,
    REVIEW_ROW_COLOR,
    TAB_REVIEW,
    ReviewEntry,
    build_review_row,
    build_tooltip,
    find_row as find_review_row,
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

# Dossier Drive ou atterrit tout document qui n'a pas produit d'ecriture
# comptable : type non reconnu, ou decision humaine en attente. Le client
# doit pouvoir ouvrir la piece immediatement, meme si sa comptabilisation
# attend encore.
REVIEW_DRIVE_FOLDER = "A verifier"

# Marqueur ecrit dans la colonne ICE d'une fiche tiers creee sans ICE. Une
# valeur explicite vaut mieux qu'une cellule vide : elle se filtre, elle se
# retrouve, et elle dit ce qu'il reste a faire.
ICE_TO_COMPLETE = "A completer"

# Fond bleu clair des lignes REELLEMENT creees par le bot. Repere visuel
# seulement : aucune valeur comptable n'en depend, et les regles de mise en
# forme conditionnelle du classeur restent prioritaires.
NEW_ROW_COLOR = "#DDEBF7"

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

    def upload(self, *, name: str, mimetype: str, content: bytes) -> str: ...


@dataclass
class PartyMatch:
    party_id: str = ""
    name: str = ""
    existing: bool = False
    ambiguous: bool = False
    reason: str = ""


def _now_iso() -> str:
    """Horodatage UTC, meme convention que le journal d'import."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    # Signalements non bloquants (ICE a completer, champ secondaire absent).
    warnings: list[str] = field(default_factory=list)
    # Renseigne pour un document en attente de decision : il est deja
    # archive, la seule chose qui manque est l'ecriture comptable.
    pending_review: bool = False

    @property
    def type_label(self) -> str:
        return LABELS.get(self.doc_type, self.doc_type)


def _stamp_micro() -> str:
    """Horodatage UTC a la microseconde, utilisable comme nom d'onglet."""
    return (
        datetime.now(timezone.utc).isoformat()
        .replace(":", "-").replace(".", "-")
    )


def _trim(lignes: list[list[Any]]) -> list[list[Any]]:
    """Retire les cellules vides de fin de chaque ligne.

    Une lecture A1:Z rend des lignes completees a vingt-six colonnes. Les
    recopier telles quelles ferait d'une restauration une ligne de huit
    colonnes suivie de dix-huit cellules vides : le contenu serait le bon,
    l'onglet ne serait plus le meme.
    """
    propres: list[list[Any]] = []
    for ligne in lignes:
        copie = list(ligne)
        while copie and str(copie[-1]) == "":
            copie.pop()
        propres.append(copie)
    return propres


def _last_column(lignes: list[list[Any]]) -> str:
    """Derniere colonne REELLEMENT occupee par un bloc de lignes.

    Ecrire systematiquement jusqu'a Z ajouterait des cellules vides a
    droite de chaque ligne restauree : le contenu serait juste, la
    comparaison avec l'original ne le serait plus.
    """
    largeur = max((len(l) for l in lignes), default=1)
    return chr(ord("A") + max(0, min(largeur, 26) - 1))


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
        allowed_vat_rates: tuple[Decimal, ...] | None = None,
        today: Callable[[], date] | None = None,
        vision: Any | None = None,
        vision_budget: Any | None = None,
        company_id: str = "",
        account_mapping: dict | None = None,
        company_ice: str = "",
    ) -> None:
        self._gw = gateway
        self._db = db_path
        self._chat_id = chat_id
        # Entreprise proprietaire de CE traitement. Toute recherche d'etat
        # (doublon, jumeau ouvert, empreinte bancaire, quarantaine) est
        # enfermee dedans : une piece envoyee a deux societes doit produire
        # une ecriture dans CHACUNE, et jamais une seule "deja vue".
        # Une chaine vide preserve exactement le comportement mono-entreprise
        # d'avant la V2, le temps que la migration ait tourne.
        self._company_id = company_id
        # Plan de comptes complementaire de la societe (banque, frais...).
        # Vide par defaut : les operations qui l'exigent partent A_VALIDER.
        self._account_mapping = dict(account_mapping or {})
        # ICE legal de la societe tenant : sert au controle d'ORIENTATION
        # (achat/vente) contre les deux parties du document. Vide = controle
        # inapplicable (mono-entreprise historique), comportement inchange.
        self._company_ice = (company_ice or "").strip()
        self._sheet = spreadsheet_id
        self._company = company
        self._drive_root = drive_root
        self._tabs_cache: list[str] | None = None
        self._folder_cache: dict[str, str] = {}
        # Taux de TVA autorises et horloge, INJECTES. Le pipeline ne lit ni
        # la configuration ni l'heure au moment de decider : sans cela, un
        # test de "facture datee dans le futur" cesserait de passer le jour
        # ou cette date devient le passe.
        self._vat_rates = tuple(allowed_vat_rates) if allowed_vat_rates else ()
        self._today = today or date.today
        # Escalade de lecture (Terra puis Sol). Absente => comportement
        # deterministe strictement inchange.
        self._vision = vision
        self._vision_budget = vision_budget

    @property
    def company_id(self) -> str:
        return self._company_id

    def _scope(self) -> dict[str, str]:
        """Portee entreprise a joindre a CHAQUE recherche d'etat."""
        return {"company_id": self._company_id}

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

    def mark_new_row(self, tab: str, row_index: int, width: str = "Q") -> None:
        """Teinte en bleu clair UNE ligne reellement creee par le bot.

        SEUL le fond est ecrit : le masque de champs envoye par l'API se
        limite a userEnteredFormat.backgroundColor, donc les formats de
        date et de devise, les formules, les liens Drive, les bordures et
        les validations de la ligne restent intacts. Les regles de mise en
        forme conditionnelle du classeur (rouge doublon, orange anomalie,
        jaune impaye) sont evaluees APRES le fond de cellule : la couleur
        metier garde donc naturellement la priorite sur ce bleu.

        Jamais appele sur un doublon ignore, sur une reprise idempotente ni
        sur la mise a jour d'une ligne existante : ces cas ne creent pas de
        ligne.
        """
        if row_index < 2:
            return
        try:
            self._gw.execute(
                "GOOGLESHEETS_FORMAT_CELL",
                {
                    "spreadsheet_id": self._sheet,
                    "sheet_name": tab,
                    "range": f"A{row_index}:{width}{row_index}",
                    "background_color": NEW_ROW_COLOR,
                },
            )
        except Exception as exc:  # noqa: BLE001 - le fond n'est pas comptable
            logger.warning(
                "Fond de ligne %s!%d non applique: %s", tab, row_index, exc
            )

    # -- onglet 21_A_VERIFIER ----------------------------------------------

    def ensure_review_tab(self) -> int:
        """Cree `21_A_VERIFIER` s'il manque et renvoie son identifiant.

        L'onglet entier est force en TEXTE. C'est volontaire et c'est le
        coeur de la garantie : un montant douteux ecrit ici est une chaine
        de caracteres, pas un nombre. Aucune somme, aucune formule de TVA
        et aucune tuile du Dashboard ne peut le capter, meme par accident.
        """
        if TAB_REVIEW not in self.tabs():
            self._gw.execute(
                "GOOGLESHEETS_ADD_SHEET",
                {"spreadsheet_id": self._sheet, "title": TAB_REVIEW,
                 "force_unique": False},
            )
            self._write(f"{TAB_REVIEW}!A1:{REVIEW_LAST_COL}1", [REVIEW_HEADERS])
            self._tabs_cache = None
            try:
                self._gw.execute(
                    "GOOGLESHEETS_FORMAT_CELL",
                    {
                        "spreadsheet_id": self._sheet,
                        "sheet_name": TAB_REVIEW,
                        "range": f"A2:{REVIEW_LAST_COL}500",
                        "number_format_type": "TEXT",
                        "wrap_strategy": "CLIP",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - format non bloquant
                logger.warning("Format texte de %s non applique: %s", TAB_REVIEW, exc)
            logger.info("Onglet %s cree (zone de quarantaine comptable)", TAB_REVIEW)
        return self.sheet_id(TAB_REVIEW)

    def backup_review_tab(self) -> str:
        """Copie l'onglet de quarantaine dans un onglet horodate.

        Faite AVANT toute reconstruction. On copie les valeurs telles
        quelles : le but n'est pas de rejouer la mise en forme, mais de
        pouvoir relire ce qui existait si la reconstruction deraille.

        Rend le nom de l'onglet de sauvegarde. Leve si la copie n'a pas
        pu etre relue - une sauvegarde non verifiee ne compte pas.
        """
        if TAB_REVIEW not in self.tabs(refresh=True):
            # Installation neuve : il n'y a rien a proteger. Ce n'est pas
            # un echec, et cela ne doit pas bloquer la migration.
            logger.info("Onglet %s absent : aucune sauvegarde necessaire", TAB_REVIEW)
            return ""
        lignes = self._read(f"{TAB_REVIEW}!A1:{REVIEW_LAST_COL}5000")
        if len(lignes) <= 1:
            # En-tete seul, ou onglet vide : rien de metier a sauvegarder.
            logger.info("Onglet %s sans ligne de donnees : rien a sauvegarder", TAB_REVIEW)
            return ""

        nom = f"{TAB_REVIEW}_BACKUP_{_now_iso().replace(':', '-')}"
        self._gw.execute(
            "GOOGLESHEETS_ADD_SHEET",
            {"spreadsheet_id": self._sheet, "title": nom, "force_unique": False},
        )
        self._tabs_cache = None
        fin = len(lignes)
        self._write(f"{nom}!A1:{REVIEW_LAST_COL}{fin}", lignes)

        # Verification : on relit la copie et on compare le nombre de
        # lignes. Sans cela, on croirait avoir un filet sans en avoir un.
        relu = self._read(f"{nom}!A1:{REVIEW_LAST_COL}5000")
        if len(relu) != fin:
            raise PipelineError(
                f"Sauvegarde {nom} incomplete : {len(relu)} lignes relues "
                f"sur {fin} attendues."
            )
        logger.info(
            "Onglet %s sauvegarde dans %s | %d ligne(s) relues", TAB_REVIEW, nom, fin
        )
        return nom

    def clear_review_rows(self) -> int:
        """Vide les LIGNES de `21_A_VERIFIER`, jamais son en-tete.

        Les 12 en-tetes et la mise en forme de l'onglet restent en place :
        on efface des valeurs, on ne detruit pas la structure.
        """
        if TAB_REVIEW not in self.tabs():
            return 0
        lignes = self._read(f"{TAB_REVIEW}!A2:A5000")
        if not lignes:
            return 0
        fin = len(lignes) + 1
        self._gw.execute(
            "GOOGLESHEETS_CLEAR_VALUES",
            {
                "spreadsheet_id": self._sheet,
                "range": f"{TAB_REVIEW}!A2:{REVIEW_LAST_COL}{fin}",
            },
        )
        logger.info("Onglet %s : %d ligne(s) effacees", TAB_REVIEW, len(lignes))
        return len(lignes)

    def write_review(self, entry: ReviewEntry) -> int:
        """Ecrit - ou REECRIT - la ligne de quarantaine d'un document.

        Idempotent par construction : un document deja present dans
        l'onglet voit sa ligne mise a jour, jamais dupliquee. C'est
        indispensable, car un document ecarte est reexamine a chaque cycle
        Gmail.

        Renvoie le numero de ligne ecrit.
        """
        sheet_id = self.ensure_review_tab()
        existing = [
            str(r[0]).strip() if r else ""
            for r in self._read(f"{TAB_REVIEW}!A2:A2000")
        ]
        row_index = find_review_row(existing, entry.doc_key)
        nouvelle = row_index == 0
        if nouvelle:
            row_index = len(existing) + 2
        self._write(
            f"{TAB_REVIEW}!A{row_index}:{REVIEW_LAST_COL}{row_index}",
            [build_review_row(entry)],
        )
        self._paint_review_row(row_index)
        self._explain_review_row(sheet_id, row_index, entry)
        logger.info(
            "Document %s %s dans %s ligne %d : %s",
            entry.short_key, "ecrit" if nouvelle else "mis a jour",
            TAB_REVIEW, row_index, entry.reasons[0] if entry.reasons else "anomalie",
        )
        return row_index

    def _paint_review_row(self, row_index: int) -> None:
        """Fond rouge de la ligne. Jamais bloquant : une couleur absente
        ne doit pas empecher un document douteux d'etre signale."""
        try:
            self._gw.execute(
                "GOOGLESHEETS_FORMAT_CELL",
                {
                    "spreadsheet_id": self._sheet,
                    "sheet_name": TAB_REVIEW,
                    "range": f"A{row_index}:{REVIEW_LAST_COL}{row_index}",
                    "background_color": REVIEW_ROW_COLOR,
                },
            )
        except Exception as exc:  # noqa: BLE001 - la couleur n'est pas comptable
            logger.warning(
                "Fond rouge de %s!%d non applique: %s", TAB_REVIEW, row_index, exc
            )

    def _explain_review_row(
        self, sheet_id: int, row_index: int, entry: ReviewEntry
    ) -> None:
        """Pose l'explication au survol de la cellule Anomalie.

        La passerelle Composio n'expose PAS l'ecriture d'une note de
        cellule native (`spreadsheets.batchUpdate` / `repeatCell.note`).
        On utilise donc le seul mecanisme disponible qui affiche un texte
        au survol : une regle de validation NON bloquante (`strict=False`)
        dont le message d'aide porte le motif. Le detail complet reste de
        toute facon lisible en clair dans la colonne I, qui n'a ni limite
        de longueur ni dependance a ce mecanisme.
        """
        index = ord(COL_ANOMALY) - ord("A")
        try:
            self._gw.execute(
                "GOOGLESHEETS_SET_DATA_VALIDATION_RULE",
                {
                    "spreadsheet_id": self._sheet,
                    "sheet_id": sheet_id,
                    "mode": "SET",
                    "validation_type": "NOT_BLANK",
                    "strict": False,
                    "show_custom_ui": False,
                    "input_message": build_tooltip(entry),
                    "start_row_index": row_index - 1,
                    "end_row_index": row_index,
                    "start_column_index": index,
                    "end_column_index": index + 1,
                },
            )
        except Exception as exc:  # noqa: BLE001 - l'infobulle n'est pas comptable
            logger.warning(
                "Infobulle de %s!%s%d non posee: %s",
                TAB_REVIEW, COL_ANOMALY, row_index, exc,
            )

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
        if not wanted_ice:
            # Facture sans ICE. Le nom normalise devient le seul point
            # d'ancrage : unique, on reutilise la fiche existante ; multiple,
            # on demande, car imputer au mauvais tiers fausse la comptabilite.
            if len(by_name) == 1:
                return PartyMatch(
                    str(by_name[0][0]).strip(), str(by_name[0][1]).strip(), existing=True
                )
            if len(by_name) > 1:
                return PartyMatch(
                    ambiguous=True,
                    reason=f"{len(by_name)} tiers existants portent le nom '{name}'",
                )
        elif by_name:
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
        self.mark_new_row(tab, row_index, "G")
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

    def archive(self, file: DocumentFile, folder: str, year: int, source_url: str,
                *, month: int = 0, gmail_message_id: str = "",
                reference: str = "", statut: str = "") -> str:
        """Archive la piece dans Entreprise/AAAA/MM/<categorie>, une seule fois.

        Le contenu archive est celui du document lui-meme, octets
        inchanges. Le registre `drive_archives` rend l'operation
        idempotente PAR CONTENU : les memes octets deja archives dans
        cette entreprise rendent le lien existant sans second depot -
        et les memes octets dans une AUTRE entreprise s'archivent chez
        elle, la cle du registre etant (entreprise, empreinte).
        """
        if self._company_id and file.sha256:
            connue = archive_log.known(self._db, self._company_id, file.sha256)
            if connue and connue.get("drive_link"):
                return str(connue["drive_link"])
        target = self.archive_folder(folder, year, month)
        if file.content:
            uploaded = self._upload_content(file, target)
            if uploaded:
                self._remember_archive(file, uploaded, folder,
                                       gmail_message_id, reference, statut)
                return uploaded
        if file.source != "attachment" or not source_url:
            raise PipelineError(
                f"Archivage impossible pour {file.display_name} : "
                "le contenu du document n'est pas disponible."
            )
        args: dict[str, Any] = {
            "source_url": source_url,
            "name": file.filename,
            "mime_type": content_mimetype(file.content) if file.content else "application/pdf",
        }
        if target:
            args["parent_folder_id"] = target
        uploaded = self._gw.execute("GOOGLEDRIVE_UPLOAD_FROM_URL", args)
        lien = drive_link(uploaded)
        if lien:
            self._remember_archive(file, lien, folder,
                                   gmail_message_id, reference, statut)
        return lien

    def archive_original_bundle(self, name: str, content: bytes,
                                gmail_message_id: str) -> str:
        """Archive le ZIP recu, tel quel, dans Emails_ZIP.

        Chaque document extrait a deja son archivage individuel ; le
        ZIP est la piece probante de la reception. Une seule fois par
        contenu et par entreprise, via le registre.
        """
        if not content:
            return ""
        empreinte = hashlib.sha256(content).hexdigest()
        if self._company_id:
            connue = archive_log.known(self._db, self._company_id, empreinte)
            if connue and connue.get("drive_link"):
                return str(connue["drive_link"])
        aujourdhui = date.today()
        cible = self.archive_folder("Emails_ZIP", aujourdhui.year, aujourdhui.month)
        upload = getattr(self._gw, "upload", None)
        if upload is None:
            return ""
        cle = upload(name=name, mimetype="application/zip", content=content)
        if not cle:
            return ""
        args: dict[str, Any] = {
            "file_to_upload": {"name": name, "mimetype": "application/zip",
                               "s3key": cle}
        }
        if cible:
            args["folder_to_upload_to"] = cible
        lien = drive_link(self._gw.execute("GOOGLEDRIVE_UPLOAD_FILE", args))
        if lien and self._company_id:
            archive_log.remember(
                self._db, company_id=self._company_id, sha256=empreinte,
                original_name=name, mimetype="application/zip",
                size_bytes=len(content), gmail_message_id=gmail_message_id,
                statut="zip-original", category="Emails_ZIP",
                drive_file_id=drive_file_id(lien) or "", drive_link=lien,
            )
        return lien

    def _remember_archive(self, file: DocumentFile, lien: str, category: str,
                          gmail_message_id: str, reference: str, statut: str) -> None:
        """Inscrit l'archive au registre. Jamais bloquant : une panne du
        registre ne doit pas faire perdre un depot qui, lui, a reussi."""
        if not self._company_id or not file.sha256:
            return
        try:
            archive_log.remember(
                self._db, company_id=self._company_id, sha256=file.sha256,
                original_name=file.filename,
                mimetype=content_mimetype(file.content) if file.content else "",
                size_bytes=len(file.content or b""),
                gmail_message_id=gmail_message_id, reference=reference,
                statut=statut, category=category,
                drive_file_id=drive_file_id(lien) or "", drive_link=lien,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Registre d'archives indisponible : %s", type(exc).__name__)

    def _root_folder(self) -> str:
        """Racine d'archivage de l'entreprise.

        En multi-tenant elle arrive comme un IDENTIFIANT Drive, pas un
        nom : le chercher par nom creerait un dossier homonyme errant a
        la racine du Drive - c'est arrive lors de la premiere validation
        E2E. Un identifiant se reconnait et s'utilise tel quel.
        """
        racine = str(self._drive_root or "").strip()
        if len(racine) >= 20 and " " not in racine and "/" not in racine:
            return racine
        return self.ensure_folder(racine)

    def archive_folder(self, folder: str, year: int, month: int = 0) -> str:
        """Dossier cible : Entreprise/AAAA/MM/Categorie.

        L'annee puis le mois puis la categorie : un exercice comptable se
        consulte par periode d'abord, par nature ensuite.
        """
        root = self._root_folder()
        annee = self.ensure_folder(str(year), root) or root
        niveau = annee
        if month:
            niveau = self.ensure_folder(f"{month:02d}", annee) or annee
        return self.ensure_folder(folder, niveau) or niveau or root

    def _upload_content(self, file: DocumentFile, target: str) -> str:
        """Depot des octets reels. Retourne "" si la passerelle ne sait pas
        deposer de contenu : l'appelant decide alors du repli."""
        upload = getattr(self._gw, "upload", None)
        if upload is None:
            return ""
        mimetype = content_mimetype(file.content)
        try:
            key = upload(
                name=file.filename, mimetype=mimetype, content=file.content
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Depot du contenu de %s impossible: %s", file.display_name, exc)
            return ""
        if not key:
            return ""
        args: dict[str, Any] = {
            "file_to_upload": {
                "name": file.filename,
                "mimetype": mimetype,
                "s3key": key,
            }
        }
        if target:
            args["folder_to_upload_to"] = target
        return drive_link(self._gw.execute("GOOGLEDRIVE_UPLOAD_FILE", args))

    def relocate(self, link: str, folder: str, year: int, month: int = 0) -> bool:
        """Deplace une piece archivee dans 'A verifier' vers son dossier
        definitif, une fois la decision humaine prise."""
        file_id = drive_file_id(link)
        target = self.archive_folder(folder, year, month)
        if not file_id or not target:
            return False
        try:
            meta = self._gw.execute(
                "GOOGLEDRIVE_GET_FILE_METADATA",
                {"fileId": file_id, "fields": "id,parents"},
            )
            parents = [str(p) for p in (meta.get("parents") or []) if p]
            if parents == [target]:
                return True
            args: dict[str, Any] = {"file_id": file_id, "add_parents": target}
            if parents:
                args["remove_parents"] = ",".join(parents)
            self._gw.execute("GOOGLEDRIVE_MOVE_FILE", args)
        except Exception as exc:  # noqa: BLE001 - classement non bloquant
            logger.warning("Deplacement Drive de %s impossible: %s", file_id, exc)
            return False
        return True

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
        parent_attachment_id: str = "",
        parent_filename: str = "",
        local_path: str = "",
    ) -> DocumentOutcome:
        """Traite UN document de bout en bout, ou reprend la ou il en etait.

        Une erreur ici ne concerne que ce document : l'appelant continue avec
        les autres pieces jointes du meme email.
        """
        user_id = f"telegram_{self._chat_id}"
        message_id = str(message.get("messageId") or message.get("id") or "")
        doc_key = idempotency_key(user_id, message_id, file.stable_ref, file.sha256)

        # Un document deja connu garde SA cle, meme si la formule a change ou
        # si Gmail a renvoye un autre `attachmentId` : le couple (email,
        # empreinte du fichier) identifie la piece une fois pour toutes.
        # Sans cela, chaque cycle recreait un document neuf, les boutons
        # pointaient vers des cles mortes et les reprises n'aboutissaient pas.
        known = store.find_by_message_and_sha(
            self._db, self._chat_id, message_id, file.sha256, **self._scope()
        )
        if known is not None:
            doc_key = known["doc_key"]
        outcome = DocumentOutcome(doc_key=doc_key, filename=file.display_name)

        existing = store.get_document(self._db, doc_key)
        if existing and existing["state"] in store.TERMINAL_STATES:
            outcome.action = ACTION_DUPLICATE
            outcome.doc_type = existing["doc_type"] or UNKNOWN
            outcome.numero = existing["numero"]
            outcome.stable_id = existing["stable_id"] or ""
            outcome.tab = existing["tab"] or ""
            outcome.row_index = int(existing["row_index"] or 0)
            outcome.reasons = ["document deja traite lors d'un cycle precedent"]
            return outcome
        resuming = bool(existing and existing["state"] in store.STATES_AFTER_SHEET)
        # Un document deja gare en quarantaine ("a verifier") est reexamine a
        # chaque cycle, ce qui est voulu : un humain peut avoir corrige la
        # piece. Mais les OCTETS n'ont pas change - la cle d'idempotence
        # inclut leur empreinte - donc une relecture Terra/Sol rendrait
        # exactement le meme resultat. La refaire toutes les cinq minutes
        # facturerait indefiniment un appel de vision pour rien.
        deja_en_quarantaine = bool(
            existing and existing["state"] == store.NEEDS_REVIEW
        )
        if not existing:
            store.claim_document(
                self._db, doc_key, self._chat_id,
                gmail_message_id=message_id, attachment_id=attachment_id,
                file_sha256=file.sha256, filename=file.filename,
                container=file.container,
                parent_attachment_id=parent_attachment_id or attachment_id,
                parent_filename=parent_filename or file.container or file.filename,
                member_path=file.member_path, local_path=local_path,
                **self._scope(),
            )
        else:
            # Reprise : on rafraichit ce qui a pu changer cote Gmail
            # (identifiants volatils) et ce qui a pu manquer aux versions
            # precedentes du bot (chemin interne, copie locale).
            store.update_document(
                self._db, doc_key,
                attachment_id=attachment_id,
                parent_attachment_id=parent_attachment_id or attachment_id,
                parent_filename=parent_filename or file.container or file.filename,
                member_path=file.member_path,
                local_path=local_path or existing.get("local_path") or "",
            )
        store.set_state(self._db, doc_key, store.DOWNLOADED)

        # --- meme fichier, deja connu et pas encore comptabilise -----------
        # Ce controle est place AVANT l'extraction, et l'ordre est le
        # correctif lui-meme. Place apres, il n'etait jamais atteint quand
        # l'extraction levait : un PDF illisible renvoye dans un second
        # email repartait de zero et laissait une seconde trace.
        #
        # Il ne peut pas non plus etre fondu dans la deduplication
        # comptable : un document en quarantaine n'a rien ecrit, et
        # l'annoncer "deja importe" serait faux. On rattache la nouvelle
        # fiche a sa canonique, on la CONSERVE pour l'audit, et on ne
        # touche a aucun onglet.
        if not resuming:
            twin = store.find_open_twin(
                self._db, self._chat_id, file.sha256, exclude_key=doc_key,
                **self._scope()
            )
            if twin is not None:
                return self._attach_to_twin(outcome, twin)

        # --- extraction ---------------------------------------------------
        # PDF et image partagent la meme suite : seule la PORTE d'entree
        # differe (couche texte du PDF, ou OCR de l'image), choisie sur la
        # signature du contenu et non sur l'extension. Tout ce qui suit -
        # controles comptables, seuil de confiance, quarantaine - est
        # identique. Une image illisible tombe dans le meme `except` qu'un
        # PDF illisible et laisse donc, elle aussi, une ligne rouge tracable.
        try:
            if is_image(file.content):
                doc = extract_from_image_bytes(file.content, company=self._company)
            else:
                doc = extract_from_pdf_bytes(file.content, company=self._company)
            if not deja_en_quarantaine:
                self.escalate_reading(doc, file)
        except Exception as exc:  # noqa: BLE001 - PDF/image illisible
            # Un document illisible n'est pas un document perdu. Il laisse
            # UNE ligne rouge, comme toute piece que le bot refuse de
            # comptabiliser : sans elle, le comptable ne saurait jamais
            # qu'un fichier lui est parvenu sans avoir pu etre lu.
            store.set_state(self._db, doc_key, store.FAILED, error=str(exc))
            outcome.action = ACTION_REVIEW
            outcome.pending_review = True
            outcome.error = str(exc)
            outcome.reasons = [f"document illisible : {exc}"]
            fiche = store.get_document(self._db, doc_key) or {}
            self._quarantine(outcome, message, fiche)
            return outcome

        outcome.document = doc
        outcome.doc_type = doc.doc_type

        # --- recu sans numero externe --------------------------------------
        # Un recu n'est pas rejete au SEUL motif qu'il ne porte pas de
        # numero : le numero externe reste vide et un identifiant INTERNE
        # deterministe est derive de (entreprise, email, membre, empreinte).
        # Meme piece => meme identifiant, a chaque cycle et apres redemarrage.
        # Il n'est JAMAIS presente comme un numero legal du fournisseur ;
        # toutes les autres exigences (date, montant, rapprochement unique)
        # restent entieres.
        if doc.doc_type == PAYMENT_RECEIPT and not doc.numero:
            graine = "|".join((
                self._company_id, str(message_id or ""),
                file.member_path or "", file.sha256 or "",
            ))
            doc.numero_interne = (
                "REC-INT-"
                + hashlib.sha256(graine.encode("utf-8")).hexdigest()[:10].upper()
            )
            doc.missing = [m for m in doc.missing if m != "numero"]

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
            same_file = store.find_by_sha256(
                self._db, self._chat_id, file.sha256, **self._scope()
            )
            if same_file and same_file["doc_key"] != doc_key:
                duplicates.certain = True
                duplicates.existing_ref = same_file["stable_id"] or same_file["doc_key"][:12]
                duplicates.existing_key = str(same_file["doc_key"])
            else:
                same_business = store.find_by_business_key(
                    self._db, self._chat_id, doc.doc_type, doc.numero or "",
                    **self._scope()
                )
                if same_business and same_business["doc_key"] != doc_key:
                    duplicates.certain = True
                    duplicates.existing_ref = same_business["stable_id"] or ""
                    duplicates.existing_key = str(same_business["doc_key"])

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
        # La resolution ne depend plus de la presence d'un ICE : un nom
        # lisible suffit a retrouver ou creer une fiche. Auparavant, une
        # facture sans ICE n'avait tout simplement pas de tiers.
        if needs_party and (party_ice or (party_name or "").strip()):
            party = self.resolve_party(party_tab, party_ice, party_name)

        # --- rapprochement d'un recu ---------------------------------------
        receipt_matches: list[tuple[str, int, list[Any]]] = []
        if doc.doc_type == PAYMENT_RECEIPT:
            receipt_matches = self.find_invoice(doc)

        # --- avoir : la facture d'origine doit EXISTER chez CE tenant ------
        # La reference est lue sur la piece ; on verifie qu'elle designe une
        # facture reellement comptabilisee dans CETTE societe. `None` =
        # recherche non applicable (pas d'avoir, ou reference deja jugee
        # absente par la politique).
        credit_targets: int | None = None
        if (doc.doc_type in (SUPPLIER_CREDIT_NOTE, CLIENT_CREDIT_NOTE)
                and (doc.facture_liee or "").strip()):
            credit_targets = len(self.find_origin_invoice(doc))

        context = DecisionContext(
            duplicates=duplicates,
            party_ambiguous=party.ambiguous,
            party_reason=party.reason,
            receipt_matches=len(receipt_matches),
            credit_note_targets=credit_targets,
            today=self._today(),
            allowed_vat_rates=self._vat_rates or None,
            company_ice=self._company_ice,
        )
        decision = decide(doc, context)
        outcome.action = decision.action
        outcome.reasons = list(decision.reasons)
        outcome.warnings = list(decision.warnings)
        outcome.tiers = party.name or party_name or ""

        if decision.action == ACTION_DUPLICATE:
            # Meme validee, une facture deja enregistree n'est jamais ecrite
            # deux fois : ce serait une double ecriture comptable.
            #
            # La fiche est CONSERVEE et rattachee a sa canonique. Sans ce
            # rattachement, l'audit voyait bien qu'un second exemplaire
            # etait arrive, mais rien ne disait duquel il etait le double.
            store.set_state(self._db, doc_key, store.DUPLICATE)
            if duplicates.existing_key:
                store.update_document(
                    self._db, doc_key, superseded_by=duplicates.existing_key
                )
            outcome.stable_id = decision.existing_ref
            return outcome

        if decision.action == ACTION_REVIEW:
            # Le document est ARCHIVE tout de suite, meme si sa
            # comptabilisation attend une decision : le client doit pouvoir
            # ouvrir la piece sans rouvrir sa boite mail. Seule la ligne
            # comptable manque, et le journal le dit.
            outcome.pending_review = True
            self._park(outcome, doc, file, message, source_url, store.NEEDS_REVIEW,
                       payload=json.dumps({"reasons": decision.reasons}, ensure_ascii=False))
            return outcome

        if decision.action == ACTION_UNKNOWN:
            # Aucune ecriture comptable : le document part dans "A verifier".
            self._park(outcome, doc, file, message, source_url, store.SKIPPED)
            return outcome

        store.set_state(self._db, doc_key, store.VALIDATED)
        return self._write_document(
            outcome, doc, file, message, party, party_tab, route, receipt_matches, source_url,
            resuming=resuming, existing=existing or {},
        )

    def _write_document(
        self, outcome, doc, file, message, party, party_tab, route, receipt_matches,
        source_url, *, resuming: bool, existing: dict[str, Any],
    ) -> DocumentOutcome:
        """Les ecritures proprement dites, chacune avec son point de reprise."""
        doc_key = outcome.doc_key

        # Verrou final : on refuse d'ecrire ce qui ne doit pas l'etre, meme
        # si on est arrive jusqu'ici. Un document recale ici n'est pas
        # perdu - il part en quarantaine comme les autres.
        try:
            assert_writable(doc, party.party_id)
        except NotWritable as exc:
            logger.warning(
                "Ecriture refusee pour %s (%s) : %s",
                outcome.filename, doc_key[:12], exc,
            )
            outcome.action = ACTION_REVIEW
            outcome.pending_review = True
            outcome.reasons = list(outcome.reasons) + [str(exc)]
            self._park(
                outcome, doc, file, message, source_url, store.NEEDS_REVIEW,
                payload=json.dumps({"reasons": outcome.reasons}, ensure_ascii=False),
            )
            return outcome

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
                    outcome, doc, party, party_tab, route, receipt_matches
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
        parked = bool(state.get("review_archive"))
        if outcome.drive_link and parked:
            # La piece attendait dans 'A verifier'. La decision est prise :
            # elle rejoint son dossier definitif et la ligne comptable
            # recoit enfin son lien.
            if self.relocate(outcome.drive_link, route.drive_folder,
                             (doc.date_document or date.today()).year,
                             (doc.date_document or date.today()).month):
                store.update_document(self._db, doc_key, review_archive=0)
            self._backfill(outcome.tab, outcome.row_index,
                           DRIVE_LINK_COLUMN, outcome.drive_link)
        if not outcome.drive_link:
            try:
                quand = doc.date_document or date.today()
                outcome.drive_link = self.archive(
                    file, route.drive_folder, quand.year, source_url,
                    month=quand.month,
                    gmail_message_id=str(message.get("messageId") or ""),
                    reference=doc.numero or outcome.stable_id,
                    statut="comptabilise",
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

        # --- etape 3ter : ecritures comptables ------------------------------
        # L'idempotence est portee par le journal lui-meme (cle par piece) :
        # un rejeu du meme document rend record()==False et ne touche ni la
        # base ni le classeur. La facture et son paiement restent deux
        # evenements distincts - le paiement s'ecrit au rapprochement.
        self._post_ledger_entry(doc, outcome, file,
                                str(message.get("messageId") or ""))

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
        log_row = int(state.get("log_row") or 0)
        if not log_row:
            log_row = self._safe_log(outcome, message)
            store.update_document(self._db, doc_key, log_row=log_row, state=store.LOGGED)
        elif parked:
            # La ligne existante disait "A valider" : elle doit maintenant
            # dire ou l'ecriture a ete faite, sans creer de seconde ligne.
            self._safe_log(outcome, message, row_index=log_row)

        store.set_state(self._db, doc_key, store.COMPLETED)
        return outcome

    def _write_primary(
        self, outcome, doc, party, party_tab, route, receipt_matches
    ) -> None:
        """Ecrit la ligne principale, selon le type de document."""
        kind = doc.doc_type
        year = (doc.date_document or date.today()).year

        if kind in (PURCHASE_INVOICE, SALES_INVOICE, IMPORT_INVOICE, EXPORT_INVOICE):
            if party.party_id and not party.existing:
                ice = doc.emetteur_ice if is_purchase_side(kind) else doc.destinataire_ice
                # Fiche provisoire quand l'ICE manque : l'identifiant interne
                # existe, la comptabilite tient, et la colonne ICE porte ce
                # qu'il reste a faire au lieu d'une cellule vide muette.
                self.create_party(party_tab, party, ice or ICE_TO_COMPLETE)
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
            written, start = self.write_bank_statement(doc, doc_key=outcome.doc_key)
            outcome.tab = TAB_BANK
            outcome.row_index = start
            outcome.stable_id = doc.numero or f"REL-{year}"
            outcome.reasons.append(f"{written} operation(s) bancaire(s) ecrite(s)")
            if written:
                # Le rapprochement ne bloque JAMAIS l'ecriture du releve : une
                # operation qu'on ne sait pas rattacher reste "Non rapproche".
                try:
                    rapproches = self.reconcile_bank_lines(doc, first_row=start)
                except Exception as exc:  # noqa: BLE001 - jamais bloquant
                    logger.warning("Rapprochement bancaire non effectue : %s", exc)
                else:
                    if rapproches:
                        outcome.reasons.append(
                            f"{len(rapproches)} rapprochement(s) : "
                            + ", ".join(r["numero"] for r in rapproches)
                        )
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
            # L'identifiant interne deterministe supplee un numero externe
            # absent - il n'est jamais presente comme un numero legal.
            outcome.stable_id = doc.numero or doc.numero_interne or ""
            outcome.reasons.append(f"facture soldee dans {tab} ligne {row_index}")
            return

        raise PipelineError(f"Type '{kind}' sans regle d'ecriture.")

    def _park(
        self,
        outcome: DocumentOutcome,
        doc: ExtractedDocument,
        file: DocumentFile,
        message: dict[str, Any],
        source_url: str,
        state: str,
        *,
        payload: str = "",
    ) -> None:
        """Range un document SANS ecriture comptable : Drive puis journal.

        Archivage et journal sont faits UNE SEULE FOIS. Un document en
        attente de decision est reexamine a chaque cycle : sans ce garde-fou,
        chaque tour de boucle deposait une copie de plus dans Drive et une
        ligne de plus dans le journal d'import.
        """
        known = store.get_document(self._db, outcome.doc_key) or {}
        outcome.drive_link = str(known.get("drive_link") or "")
        if not outcome.drive_link:
            outcome.drive_link = self._archive_for_review(
                file, doc, source_url, outcome.doc_key
            )
        fields: dict[str, Any] = {
            "state": state,
            "drive_link": outcome.drive_link,
            # L'archive est provisoire : elle vit dans "A verifier" tant
            # qu'aucune decision n'est prise.
            "review_archive": 1 if outcome.drive_link else 0,
        }
        if payload:
            fields["payload"] = payload
        store.update_document(self._db, outcome.doc_key, **fields)
        if not known.get("log_row"):
            log_row = self._safe_log(outcome, message)
            store.update_document(self._db, outcome.doc_key, log_row=log_row)
        self._quarantine(outcome, message, known)

    def _business_identity(self, outcome: DocumentOutcome) -> str:
        """Identite comptable d'un document, telle qu'on la figera en base.

        Construite a partir de l'`outcome`, donc des valeurs REELLEMENT
        lues dans le PDF. Une identite vide est un refus de conclure : on
        preferera toujours deux lignes a une fusion abusive.
        """
        doc = outcome.document
        return business_identity({
            "doc_type": outcome.doc_type,
            "numero": outcome.numero,
            "party_id": outcome.tiers,
            "date_document": str(getattr(doc, "date_document", "") or ""),
            "montant_ttc": outcome.montant_ttc,
        })

    def _existing_review_row(self, empreinte: str, doc_key: str) -> tuple[int, str] | None:
        """Une ligne de quarantaine existe-t-elle deja pour CE document ?

        La comparaison porte sur l'identite comptable stockee, jamais sur
        le `doc_key` : celui-ci contient l'identifiant du message Gmail et
        change donc d'un email a l'autre. C'est exactement ce qui produisait
        deux lignes rouges pour une seule facture envoyee deux fois.

        Rend (ligne, cle canonique), ou None si rien de fiable.
        """
        if not empreinte:
            return None
        for fiche in store.list_quarantined(self._db, self._chat_id, **self._scope()):
            if str(fiche.get("doc_key")) == doc_key:
                continue
            if str(fiche.get("business_key") or "") == empreinte:
                return int(fiche["review_row"]), str(fiche["doc_key"])
        return None

    def _attach_to_twin(
        self, outcome: DocumentOutcome, twin: dict[str, Any]
    ) -> DocumentOutcome:
        """Rattache une relecture a la fiche canonique du meme fichier.

        Trois garanties, et la troisieme est la moins evidente :
          - aucune ecriture, nulle part ;
          - la fiche secondaire N'EST PAS supprimee : elle porte
            `superseded_by`, donc l'audit sait que ce fichier est bien
            arrive une seconde fois, et par quel email ;
          - l'`outcome` pointe vers la ligne de quarantaine DEJA existante,
            pour que le resume dise ou regarder au lieu d'annoncer un
            import qui n'a pas eu lieu.
        """
        canonique = str(twin["doc_key"])
        store.update_document(
            self._db, outcome.doc_key,
            superseded_by=canonique, state=store.SUPERSEDED,
        )
        ligne = int(twin.get("review_row") or 0)
        outcome.action = ACTION_DUPLICATE
        outcome.stable_id = str(twin.get("stable_id") or "")
        if ligne:
            outcome.tab = TAB_REVIEW
            outcome.row_index = ligne
        emplacement = f" ({TAB_REVIEW} ligne {ligne})" if ligne else ""
        outcome.reasons = [
            f"fichier identique deja enregistre sous {canonique[:12]}"
            f"{emplacement} : aucune nouvelle ligne, aucune ecriture"
        ]
        logger.info(
            "Document %s rattache a %s%s : fiche conservee, marquee superseded_by",
            outcome.doc_key[:12], canonique[:12], emplacement,
        )
        return outcome

    def _quarantine(
        self,
        outcome: DocumentOutcome,
        message: dict[str, Any],
        known: dict[str, Any],
    ) -> None:
        """Inscrit le document dans `21_A_VERIFIER`.

        C'est desormais le SEUL aboutissement d'un document douteux : plus
        de bouton, plus d'attente silencieuse. Le comptable voit la ligne
        rouge, lit le motif, et corrige lui-meme.

        L'ecriture est retentee tant qu'elle n'a pas abouti (`review_row`
        vide), et sautee ensuite : un document deja en quarantaine ne doit
        pas etre reecrit a chaque cycle Gmail.
        """
        if int(known.get("review_row") or 0):
            return

        # Une ligne par document PHYSIQUE, pas par relecture. Le meme
        # document peut revenir sous un fichier different - re-export,
        # re-scan, tampon appose - donc avec une autre empreinte. Son
        # identite COMPTABLE, elle, ne bouge pas.
        empreinte = self._business_identity(outcome)
        jumeau = self._existing_review_row(empreinte, outcome.doc_key)
        if jumeau is not None:
            ligne, canonique = jumeau
            store.update_document(
                self._db, outcome.doc_key,
                review_row=ligne, superseded_by=canonique,
            )
            outcome.tab = TAB_REVIEW
            outcome.row_index = ligne
            logger.info(
                "Document %s : meme identite metier que %s, ligne %s reutilisee "
                "dans %s (aucune ligne ajoutee)",
                outcome.doc_key[:12], canonique[:12], ligne, TAB_REVIEW,
            )
            return

        entry = ReviewEntry(
            doc_key=outcome.doc_key,
            detected_at=_now_iso(),
            type_label=outcome.type_label,
            numero=outcome.numero or "",
            tiers=outcome.tiers or "",
            devise=outcome.devise or "",
            montant_ht=outcome.montant_ht,
            montant_tva=outcome.montant_tva,
            montant_ttc=outcome.montant_ttc,
            reasons=list(outcome.reasons),
            drive_link=outcome.drive_link or "",
            gmail_message_id=str(message.get("id") or message.get("messageId") or ""),
            filename=outcome.filename,
        )
        try:
            row_index = self.write_review(entry)
        except Exception as exc:  # noqa: BLE001 - le cycle Gmail ne meurt jamais
            logger.warning(
                "Document %s non inscrit dans %s (reessai au prochain cycle): %s",
                entry.short_key, TAB_REVIEW, exc,
            )
            return
        store.update_document(
            self._db, outcome.doc_key,
            review_row=row_index, business_key=empreinte,
        )

    def _archive_for_review(
        self, file: DocumentFile, doc: ExtractedDocument, source_url: str, doc_key: str
    ) -> str:
        """Depose une piece dans Drive / 'A verifier' et renvoie son lien.

        Un echec d'archivage ne doit jamais faire disparaitre la demande de
        validation : on journalise et on rend un lien vide.
        """
        try:
            quand = doc.date_document or date.today()
            return self.archive(
                file, REVIEW_DRIVE_FOLDER, quand.year, source_url,
                month=quand.month, reference=doc.numero or doc_key[:12],
                statut="quarantaine",
            )
        except Exception as exc:  # noqa: BLE001 - archivage non bloquant
            logger.warning(
                "Archivage 'A verifier' impossible (%s): %s", doc_key[:12], exc
            )
            return ""

    _LEDGER_KINDS = {
        PURCHASE_INVOICE: "facture_achat",
        IMPORT_INVOICE: "facture_achat",
        SALES_INVOICE: "facture_vente",
        EXPORT_INVOICE: "facture_vente",
        SUPPLIER_CREDIT_NOTE: "avoir_fournisseur",
        CLIENT_CREDIT_NOTE: "avoir_client",
    }

    def _ledger_mapping(self) -> "ledger.AccountMapping":
        return ledger.AccountMapping(getattr(self, "_account_mapping", None))

    def _post_ledger_entry(self, doc, outcome, file, gmail_message_id: str) -> None:
        """Pose l'ecriture en partie double d'un document comptabilise.

        Seuls les documents aux montants complets et coherents arrivent
        ici - les autres sont partis en quarantaine bien avant. Si un
        desequilibre survenait quand meme, l'ecriture serait enregistree
        A_VALIDER avec l'ecart exact, jamais posee comme definitive, et
        la piece resterait tracable. Une panne du journal ne casse pas le
        traitement : le document est deja ecrit et archive.
        """
        kind = self._LEDGER_KINDS.get(doc.doc_type)
        if kind is None or not self._company_id or not doc.numero:
            return
        try:
            ht = doc.montant_ht.value if doc.montant_ht else None
            tva = doc.montant_tva.value if doc.montant_tva else Decimal("0")
            ttc = doc.montant_ttc.value if doc.montant_ttc else None
            if ht is None or ttc is None:
                return
            # Un avoir arrive souvent avec des montants NEGATIFS : le
            # generateur inverse deja les comptes, garder le signe
            # inverserait l'effet une seconde fois (recap TVA fausse).
            ht, tva, ttc = abs(ht), abs(tva), abs(ttc)
            try:
                entry = ledger.build_entry(
                    self._ledger_mapping(), company_id=self._company_id,
                    kind=kind, piece=doc.numero,
                    entry_date=(doc.date_document or date.today()).isoformat(),
                    devise=doc.devise or "MAD", ht=ht, tva=tva, ttc=ttc,
                    tiers=(doc.emetteur or doc.destinataire or ""),
                    taux_tva=str(doc.taux_tva or ""),
                    reference=outcome.stable_id or "",
                    doc_sha256=file.sha256, gmail_message_id=gmail_message_id,
                    drive_file_id=drive_file_id(outcome.drive_link) or "",
                )
            except ledger.LedgerImbalance as exc:
                entry = ledger.Entry(
                    company_id=self._company_id, journal=ledger.JOURNAL_OD,
                    piece=doc.numero,
                    entry_date=(doc.date_document or date.today()).isoformat(),
                    lines=[], tiers=(doc.emetteur or doc.destinataire or ""),
                    devise=doc.devise or "MAD",
                    doc_sha256=file.sha256, gmail_message_id=gmail_message_id,
                    statut=ledger.STATUT_A_VALIDER, motif=str(exc),
                )
                logger.warning("Ecriture %s NON validee : %s", doc.numero, exc)
            if ledger.record(self._db, entry):
                self._append_journal(entry)
                self._write_tva_recap()
        except Exception as exc:  # noqa: BLE001 - jamais bloquant
            logger.warning("Journal comptable indisponible pour %s : %s",
                           doc.numero, type(exc).__name__)

    def _post_bank_ledger(self, *, kind: str, piece: str, montant, jour: str,
                          tiers: str, reference: str, doc_sha256: str = "") -> None:
        """Ecriture bancaire (paiement, reglement, frais).

        Le template ne declare pas de compte banque : tant que la societe
        n'en fournit pas via `account_mapping`, ces ecritures partent
        A_VALIDER - enregistrees, motivees, jamais inventees.
        """
        if not self._company_id:
            return
        try:
            entry = ledger.build_entry(
                self._ledger_mapping(), company_id=self._company_id, kind=kind,
                piece=piece, entry_date=jour, montant=montant, tiers=tiers,
                reference=reference, doc_sha256=doc_sha256,
            )
            if ledger.record(self._db, entry):
                self._append_journal(entry)
        except Exception as exc:  # noqa: BLE001 - jamais bloquant
            logger.warning("Ecriture bancaire %s impossible : %s",
                           piece, type(exc).__name__)

    def _append_journal(self, entry) -> None:
        """Projette une ecriture VALIDEE dans 12_JOURNAL_COMPTABLE (A..G).

        Les colonnes de controle du classeur (totaux, ecart) vivent plus a
        droite et se recalculent seules : on n'ecrit jamais au-dela de G.
        """
        lignes = ledger.sheet_rows(entry)
        if not lignes:
            return
        depart = self.next_row(TAB_JOURNAL)
        self._write(
            f"{TAB_JOURNAL}!A{depart}:G{depart + len(lignes) - 1}", lignes
        )

    def _write_tva_recap(self) -> None:
        """Recapitulatif TVA par periode, recalcule depuis les ecritures
        VALIDEES. L'onglet est une PROJECTION : le reecrire en entier est
        idempotent par construction."""
        recap = ledger.tva_recap(self._db, self._company_id)
        if not recap:
            return
        self.ensure_tab_with_headers(
            TAB_TVA_RECAP,
            ["Période", "TVA collectée", "TVA déductible", "TVA due"],
        )
        lignes = [[r["periode"], r["tva_collectee"], r["tva_deductible"],
                   r["tva_due"]] for r in recap]
        self._write(f"{TAB_TVA_RECAP}!A2:D{1 + len(lignes)}", lignes)

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

    def _safe_log(
        self, outcome: DocumentOutcome, message: dict[str, Any], row_index: int = 0
    ) -> int:
        try:
            return self.append_import_log(outcome, message, row_index=row_index)
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
        # Le tiers du recu peut etre lu cote emetteur (recu fournisseur) ou
        # cote destinataire ("Recu de : ..." sur un recu de vente) : les deux
        # noms sont des candidats legitimes pour le rapprochement, toujours
        # exiges EN PLUS du montant, jamais a sa place.
        payeurs = {
            normalize(nom) for nom in (doc.emetteur, doc.destinataire) if nom
        }
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
                if amount is not None and ttc == amount and party_name and party_name in payeurs:
                    matches.append((tab, offset + 2, row))
        return matches

    def find_origin_invoice(self, doc: ExtractedDocument) -> list[tuple[str, int, list[Any]]]:
        """Factures d'origine candidates pour un avoir, chez CE tenant.

        Correspondance EXACTE sur le numero d'abord, dans l'onglet du bon
        sens (achats pour un avoir fournisseur, ventes pour un avoir
        client). A defaut, un repli n'est admis que sur PLUSIEURS criteres
        concordants a la fois : meme tiers ET memes chiffres de reference.
        Jamais de rattachement par simple ressemblance de montant ou de
        libelle - un avoir dont l'origine reste introuvable part en
        quarantaine, motive.
        """
        wanted = normalize(doc.facture_liee or "")
        if not wanted:
            return []
        tab = TAB_PURCHASES if doc.doc_type == SUPPLIER_CREDIT_NOTE else TAB_SALES
        tiers_avoir = normalize(
            (doc.emetteur if doc.doc_type == SUPPLIER_CREDIT_NOTE
             else doc.destinataire) or ""
        )
        rows = self._read(f"{tab}!A2:Q400")
        exacts: list[tuple[str, int, list[Any]]] = []
        for offset, row in enumerate(rows):
            if len(row) < 5:
                continue
            if normalize(str(row[2])) == wanted:
                exacts.append((tab, offset + 2, row))
        if exacts:
            return exacts
        chiffres_voulus = "".join(c for c in wanted if c.isdigit())
        if not chiffres_voulus or not tiers_avoir:
            return []
        replis: list[tuple[str, int, list[Any]]] = []
        for offset, row in enumerate(rows):
            if len(row) < 5:
                continue
            chiffres = "".join(c for c in str(row[2]) if c.isdigit())
            meme_tiers = normalize(str(row[4])) == tiers_avoir
            if meme_tiers and chiffres and chiffres == chiffres_voulus:
                replis.append((tab, offset + 2, row))
        return replis

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
        self.mark_new_row(tab, row_index, "Q")
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
        """Ecrit les lignes de detail d'UNE facture, une seule fois.

        Deux garde-fous, tous deux nes d'un incident reel : le bloc de
        detail de FAC-V3-ACH-002 s'est retrouve avec une premiere ligne
        appartenant a un autre document, ecrite sous le meme identifiant
        comptable par un second ecrivain concurrent.

        1. Idempotence : si des lignes existent deja pour cet identifiant,
           on n'en ajoute pas d'autres. Un bloc partiel vaut mieux qu'un
           bloc melange, et il est visible.
        2. Coherence : on refuse d'ecrire un detail dont la somme
           contredit le total HT de la facture. Un detail faux est pire
           qu'un detail absent, parce qu'il a l'air juste.
        """
        if not doc.lignes:
            return 0
        deja = [
            r for r in self._read(f"{LIGNES_TAB}!A2:A2000")
            if r and str(r[0]).strip() == stable_id
        ]
        if deja:
            logger.info(
                "Lignes de detail de %s deja presentes (%d) : aucune reecriture",
                stable_id, len(deja),
            )
            return 0
        if doc.montant_ht is not None:
            somme = sum(
                (abs(l.total) for l in doc.lignes if l.total is not None),
                Decimal("0"),
            )
            if somme and somme != abs(doc.montant_ht.value):
                logger.warning(
                    "Detail de %s non ecrit : somme des lignes %s != total HT %s",
                    stable_id, somme, abs(doc.montant_ht.value),
                )
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
        for offset in range(len(rows)):
            self.mark_new_row(LIGNES_TAB, start + offset, "I")
        return len(rows)

    def _signal_bank_repeat(
        self, doc: ExtractedDocument, account: str, line: Any, proprietaire: str
    ) -> None:
        """Inscrit un double paiement possible dans `21_A_VERIFIER`.

        La cle de la ligne derive de l'EMPREINTE du mouvement : relire le
        meme releve ne cree donc jamais une seconde alerte pour le meme
        double paiement. Aucun montant n'entre en comptabilite : comme
        toute ligne de quarantaine, ils sont ecrits en texte.
        """
        empreinte = bank_line_fingerprint(account, line)
        libelle = str(getattr(line, "libelle", "") or "")
        montant = getattr(line, "debit", None) or getattr(line, "credit", None)
        jour = (
            line.date_operation.isoformat()
            if getattr(line, "date_operation", None) else ""
        )
        entry = ReviewEntry(
            doc_key=hashlib.sha256(
                f"double-paiement|{empreinte}".encode("utf-8")
            ).hexdigest(),
            detected_at=_now_iso(),
            type_label="Double paiement possible",
            numero=str(getattr(line, "reference", "") or ""),
            tiers=account,
            devise="MAD",
            montant_ttc=montant,
            reasons=[
                f"operation identique a une operation deja enregistree "
                f"({jour} - {libelle}) : double paiement possible",
                "les deux mouvements sont conserves ; aucun n'a ete supprime",
                f"premier enregistrement : {(proprietaire or 'releve anterieur')[:12]}",
            ],
            filename=f"{account} - {jour}",
        )
        try:
            self.write_review(entry)
        except Exception as exc:  # noqa: BLE001 - le cycle ne meurt jamais
            logger.warning(
                "Double paiement non inscrit dans %s (reessai au prochain "
                "cycle) : %s", TAB_REVIEW, exc,
            )

    def write_bank_statement(
        self, doc: ExtractedDocument, *, doc_key: str = ""
    ) -> tuple[int, int]:
        """Ecrit les operations d'un releve, et SIGNALE les repetitions.

        L'ancien comportement retirait de la liste toute operation dont
        l'empreinte etait deja connue - sans journal, sans anomalie, sans
        trace. Deux cas radicalement differents subissaient le meme sort :

          - le MEME releve reecrit (reprise, second passage) : la ligne
            existante est la bonne, il n'y a rien a ajouter ;
          - un mouvement REELLEMENT repete (deux virements identiques le
            meme jour au meme fournisseur) : c'est peut-etre un double
            paiement, et le supprimer revenait a effacer la preuve.

        Desormais le second cas est ECRIT et signale en quarantaine. Le bot
        ne decide pas s'il s'agit d'une erreur : il refuse seulement de
        faire disparaitre un mouvement bancaire.
        """
        account = doc.destinataire or "Banque Principale DEMO"
        fresh: list[Any] = []
        repetees: list[tuple[Any, str]] = []

        # Chaque OCCURRENCE d'un mouvement recoit sa propre empreinte :
        # `<empreinte>#1`, `<empreinte>#2`... Sans ce rang, deux lignes
        # identiques dans un meme releve partageaient une seule empreinte :
        # la premiere la reservait, la seconde etait consideree comme
        # "nouvelle" a CHAQUE relecture, et le rejeu du meme document
        # ajoutait une ligne de plus a chaque tour (2, puis 3, puis 4...).
        #
        # Avec le rang, la reservation couvre autant d'occurrences qu'il y
        # en a reellement : relire le meme releve n'en reserve aucune de
        # plus, donc n'ecrit plus rien.
        rangs: dict[str, int] = {}
        for line in doc.bank_lines:
            base = bank_line_fingerprint(account, line)
            rangs[base] = rangs.get(base, 0) + 1
            rang = rangs[base]
            empreinte = f"{base}#{rang}"
            repetition = rang > 1        # deja vue DANS CE RELEVE

            if store.claim_bank_line(
                self._db, self._chat_id, empreinte, doc_key=doc_key, **self._scope()
            ):
                fresh.append(line)
                if repetition:
                    # Occurrence nouvelle, mais mouvement deja vu ici : on
                    # l'ecrit ET on le signale.
                    repetees.append((line, doc_key))
                continue

            proprietaire = store.bank_line_owner(self._db, empreinte)
            if doc_key and proprietaire == doc_key:
                # Cette occurrence precise a deja ete ecrite par CE
                # document : relire n'ajoute rien, et ne realerte pas.
                logger.info(
                    "Releve %s : occurrence %d deja ecrite par ce meme "
                    "document, ignoree (aucun doublon cree)",
                    doc_key[:12], rang,
                )
                continue
            # Un AUTRE document porte deja cette operation : chevauchement
            # de releves ou paiement repete. On ecrit et on signale.
            fresh.append(line)
            repetees.append((line, proprietaire))

        for line, proprietaire in repetees:
            logger.warning(
                "Double paiement possible sur %s : operation identique a une "
                "operation deja enregistree par %s. Les DEUX mouvements sont "
                "conserves.", account, (proprietaire or "un releve anterieur")[:12],
            )
            self._signal_bank_repeat(doc, account, line, proprietaire)

        if not fresh:
            return 0, 0
        subset = ExtractedDocument(classification=doc.classification)
        subset.bank_lines = fresh
        start = self.next_row(TAB_BANK)
        rows = build_bank_rows(start_index=start - 1, doc=subset)
        self._write(f"{TAB_BANK}!A{start}:M{start + len(rows) - 1}", rows)
        for offset in range(len(rows)):
            self.mark_new_row(TAB_BANK, start + offset, "M")
        return len(rows), start

    # -- escalade de lecture Luna -> Terra -> Sol --------------------------

    def _journaliser_appel(
        self, level: str, model: str, reason: str, outcome: str,
        input_tokens: int, output_tokens: int, doc_key: str,
    ) -> None:
        """Impute UN appel de modele a l'entreprise en cours.

        Sans entreprise (mode mono-entreprise d'avant la V2) il n'y a rien
        a ventiler : on n'ecrit pas une ligne qu'on ne saurait pas lire.
        Une panne du journal ne doit jamais faire perdre une lecture qui,
        elle, a reussi.
        """
        if not self._company_id:
            return
        if not model and self._vision is not None:
            model = getattr(self._vision, "model_for", lambda _l: "")(level)
        try:
            llm_usage.record_call(
                self._db, company_id=self._company_id, level=level,
                model=model or level, doc_key=doc_key, reason=reason,
                outcome=outcome, input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=llm_usage.estimate_cost(
                    model, input_tokens, output_tokens
                ),
            )
        except Exception as exc:  # noqa: BLE001 - jamais bloquant
            logger.warning("Journal des couts indisponible : %s", type(exc).__name__)

    def escalate_reading(self, doc: ExtractedDocument, file: Any) -> None:
        """Relit un document que la lecture deterministe n'a pas su lire.

        On monte d'un niveau seulement si c'est necessaire, et on s'ARRETE
        des qu'un niveau rend un resultat qui franchit les six controles
        comptables. Sol recoit les OCTETS DE L'IMAGE ORIGINALE, jamais le
        texte OCR degrade : c'est lui le probleme.
        """
        if self._vision is None or not getattr(self._vision, "available", False):
            return
        raisons = doc_vision.escalation_reasons(doc)
        if not raisons:
            return
        logger.info(
            "Lecture a escalader (%s) : %s",
            getattr(file, "filename", "?"), ", ".join(raisons),
        )

        niveaux = []
        texte = getattr(doc, "raw_text", "") or ""
        if texte.strip():
            niveaux.append(("terra", lambda: self._vision.read_text(texte)))
        if is_image(file.content):
            niveaux.append((
                "sol",
                lambda: self._vision.read_image(file.content, content_mimetype(file.content)),
            ))

        motif = _motif_escalade(raisons)
        doc_key = getattr(file, "doc_key", "") or ""

        for nom, appel in niveaux:
            if nom == "sol":
                if self._vision_budget is not None and not self._vision_budget.take():
                    logger.warning("Budget vision epuise : %s reste en quarantaine",
                                   getattr(file, "filename", "?"))
                    doc.anomalies.append("relecture visuelle non effectuee : budget epuise")
                    return
            resultat = appel()
            # CHAQUE tentative est imputee, y compris celles qui echouent :
            # un appel refuse ou rejete a ete facture tout de meme, et une
            # escalade qui coute sans rien rendre est exactement ce qu'on
            # veut voir dans le journal.
            if resultat is None:
                self._journaliser_appel(nom, "", motif, llm_usage.OUTCOME_UNAVAILABLE,
                                        0, 0, doc_key)
                continue
            if resultat.is_empty:
                self._journaliser_appel(nom, resultat.model, motif,
                                        llm_usage.OUTCOME_EMPTY,
                                        resultat.input_tokens, resultat.output_tokens,
                                        doc_key)
                continue
            echecs = doc_vision.validate(
                resultat, today=self._today(), allowed_rates=self._vat_rates,
                allowed_currencies=("MAD",),
            )
            if echecs:
                logger.info("Niveau %s rejete pour %s : %s", nom,
                            getattr(file, "filename", "?"), " | ".join(echecs))
                self._journaliser_appel(nom, resultat.model, motif,
                                        llm_usage.OUTCOME_REJECTED,
                                        resultat.input_tokens, resultat.output_tokens,
                                        doc_key)
                continue
            self._journaliser_appel(nom, resultat.model, motif,
                                    llm_usage.OUTCOME_ACCEPTED,
                                    resultat.input_tokens, resultat.output_tokens,
                                    doc_key)
            doc_vision.apply_vision(doc, resultat)
            logger.info(
                "Niveau %s retenu pour %s : numero=%s HT=%s TVA=%s TTC=%s (confiance %.0f%%)",
                nom, getattr(file, "filename", "?"), doc.numero,
                doc.montant_ht and doc.montant_ht.value,
                doc.montant_tva and doc.montant_tva.value,
                doc.montant_ttc and doc.montant_ttc.value,
                resultat.confidence * 100,
            )
            return

    # -- rapprochement bancaire --------------------------------------------

    def reconcile_bank_lines(
        self, doc: ExtractedDocument, *, first_row: int
    ) -> list[dict[str, Any]]:
        """Rapproche les operations ecrites avec les factures deja comptabilisees.

        Une correspondance n'est retenue que si TROIS elements concordent :
        la REFERENCE citee par le libelle, le MONTANT au centime, et le SENS
        de l'operation - un debit ne peut solder qu'une facture d'achat, un
        credit qu'une facture de vente. Deux factures candidates, ou un sens
        indetermine, ne rapprochent rien : le classeur garde "Non rapproche"
        plutot qu'un lien invente.
        """
        rapproches: list[dict[str, Any]] = []
        factures = {
            TAB_PURCHASES: self._read(f"{TAB_PURCHASES}!A2:Q400"),
            TAB_SALES: self._read(f"{TAB_SALES}!A2:Q400"),
        }
        for offset, line in enumerate(doc.bank_lines):
            if not line.reference:
                # Un debit sans reference dont le libelle annonce des
                # frais est un FRAIS BANCAIRE : il recoit son ecriture
                # (A_VALIDER tant que la societe n'a pas declare ses
                # comptes banque/frais) mais ne rapproche rien.
                libelle = (line.libelle or "").lower()
                if line.debit is not None and any(
                    mot in libelle for mot in ("frais", "commission", "agios")
                ):
                    jour_frais = (line.date_operation or date.today()).isoformat()
                    self._post_bank_ledger(
                        kind="frais_bancaires",
                        piece=f"FRAIS-{jour_frais}-{abs(line.debit)}",
                        montant=abs(line.debit), jour=jour_frais,
                        tiers="Banque", reference=line.libelle or "",
                    )
                continue
            if line.debit is not None:
                tab, montant = TAB_PURCHASES, line.debit
            elif line.credit is not None:
                tab, montant = TAB_SALES, line.credit
            else:
                continue  # sens indetermine : on ne rapproche pas

            voulue = normalize(line.reference)
            candidates = []
            for index, row in enumerate(factures[tab]):
                if len(row) < 10 or not row[0]:
                    continue
                if normalize(str(row[2])) != voulue:
                    continue
                try:
                    ttc = Decimal(str(row[9]).replace("\u202f", "").replace(",", ".").split()[0])
                except Exception:  # noqa: BLE001 - cellule non numerique
                    continue
                if abs(abs(ttc) - abs(montant)) <= Decimal("0.01"):
                    candidates.append((index + 2, row))
            if len(candidates) != 1:
                continue

            row_index, row = candidates[0]
            bank_row = first_row + offset
            self._write(
                f"{TAB_BANK}!L{bank_row}:M{bank_row}",
                [[str(row[2]), "Rapproche"]],
            )
            self.settle_invoice(tab, row_index, abs(montant))
            # Le paiement est un evenement comptable DISTINCT de la
            # facture : il solde la dette via la banque, sans jamais
            # recreer la facture. Piece deterministe -> rejeu sans double.
            jour = (line.date_operation or date.today()).isoformat()
            if tab == TAB_PURCHASES:
                self._post_bank_ledger(
                    kind="paiement_fournisseur", piece=f"PAY-{row[2]}",
                    montant=abs(montant), jour=jour,
                    tiers=str(row[4]) if len(row) > 4 else "",
                    reference=str(row[2]),
                )
            else:
                self._post_bank_ledger(
                    kind="reglement_client", piece=f"ENC-{row[2]}",
                    montant=abs(montant), jour=jour,
                    tiers=str(row[4]) if len(row) > 4 else "",
                    reference=str(row[2]),
                )
            rapproches.append({
                "tab": tab, "row": row_index, "numero": str(row[2]),
                "tiers": str(row[4]) if len(row) > 4 else "",
                "montant": abs(montant), "date": str(row[1]),
                "sens": "Debit" if tab == TAB_PURCHASES else "Credit",
            })

        if rapproches:
            depart = self.next_row(TAB_RECONCILIATION)
            lignes = [[
                "Facture d'achat" if r["tab"] == TAB_PURCHASES else "Facture de vente",
                r["tab"], r["numero"], r["tiers"], to_number(r["montant"]),
                r["date"], "Oui", f"Rapproche ({r['sens']})",
            ] for r in rapproches]
            self._write(
                f"{TAB_RECONCILIATION}!A{depart}:H{depart + len(lignes) - 1}", lignes
            )
        return rapproches

    # -- outillage du nettoyage audite -------------------------------------

    def read_tab(self, tab: str) -> list[list[Any]]:
        """Contenu brut d'un onglet, en-tete compris. Lecture seule."""
        return self._read(f"{tab}!A1:Z2000")

    def ensure_tab_with_headers(self, tab: str, headers: list[str]) -> None:
        """Cree un onglet SIMPLE s'il manque. N'en modifie jamais un existant.

        Distinct de `ensure_tab`, qui reclame une specification complete de
        formats : le journal d'annulations ne porte que du texte, et lui
        inventer des formats de devise serait le rendre trompeur.
        """
        if tab in self.tabs():
            return
        self._gw.execute(
            "GOOGLESHEETS_ADD_SHEET",
            {"spreadsheet_id": self._sheet, "title": tab, "force_unique": False},
        )
        colonne = chr(ord("A") + len(headers) - 1)
        self._write(f"{tab}!A1:{colonne}1", [headers])
        self._tabs_cache = None

    def backup_tab(self, tab: str) -> str:
        """Copie relue d'UN onglet quelconque, avant toute modification.

        Generalise `backup_review_tab` a tous les onglets que le nettoyage
        touche. Une sauvegarde du seul `21_A_VERIFIER` ne protegeait pas
        `05_FACTURES_ACHATS`, `16_LIGNES_FACTURES` ni `14_IMPORTS_LOG`,
        qui sont pourtant modifies eux aussi : le rollback etait donc
        incomplet par construction.

        Rend le nom de la copie, ou une chaine vide si l'onglet n'existe
        pas. Leve si la copie ne se relit pas a l'identique.
        """
        if tab not in self.tabs(refresh=True):
            logger.info("Onglet %s absent : aucune sauvegarde necessaire", tab)
            return ""
        lignes = _trim(self._read(f"{tab}!A1:Z5000"))
        if not lignes:
            logger.info("Onglet %s vide : rien a sauvegarder", tab)
            return ""

        # Horodatage a la MICROSECONDE : deux sauvegardes prises dans la
        # meme seconde porteraient le meme nom, et la seconde ecrirait dans
        # l'onglet de la premiere - exactement ce qu'une sauvegarde ne doit
        # jamais faire.
        nom = f"{tab}_BACKUP_{_stamp_micro()}"
        if nom in self.tabs(refresh=True):
            raise PipelineError(f"un onglet nomme {nom} existe deja.")
        self._gw.execute(
            "GOOGLESHEETS_ADD_SHEET",
            {"spreadsheet_id": self._sheet, "title": nom, "force_unique": False},
        )
        self._tabs_cache = None
        derniere = _last_column(lignes)
        self._write(f"{nom}!A1:{derniere}{len(lignes)}", lignes)

        relu = _trim(self._read(f"{nom}!A1:{derniere}5000"))
        if len(relu) != len(lignes):
            raise PipelineError(
                f"Sauvegarde {nom} incomplete : {len(relu)} lignes relues "
                f"sur {len(lignes)} attendues."
            )
        # On compare aussi le CONTENU, pas seulement le compte : une copie
        # qui a le bon nombre de lignes vides ne serait pas une copie.
        if relu != lignes:
            raise PipelineError(
                f"Sauvegarde {nom} differente de l'original : restauration "
                f"impossible, aucune modification ne sera faite."
            )
        logger.info(
            "Onglet %s sauvegarde dans %s | %d ligne(s) relues et comparees",
            tab, nom, len(lignes),
        )
        return nom

    def restore_tab(self, tab: str, backup: str) -> int:
        """Restaure un onglet depuis sa copie. Rend le nombre de lignes.

        Le rollback n'est pas une idee : c'est cette fonction, et elle est
        testee en restaurant reellement des onglets modifies.
        """
        lignes = _trim(self._read(f"{backup}!A1:Z5000"))
        if not lignes:
            raise PipelineError(f"Sauvegarde {backup} vide : rien a restaurer.")
        actuelles = _trim(self._read(f"{tab}!A1:Z5000"))
        if actuelles:
            # On efface TOUTE la surface actuelle, y compris les colonnes
            # ajoutees par l'annulation : restaurer sans effacer laisserait
            # la mention "ANNULEE APRES CONTROLE" a cote des montants
            # revenus, ce qui serait pire que les deux etats separes.
            self._gw.execute(
                "GOOGLESHEETS_CLEAR_VALUES",
                {
                    "spreadsheet_id": self._sheet,
                    "range": f"{tab}!A1:{_last_column(actuelles)}"
                             f"{max(len(actuelles), len(lignes))}",
                },
            )
        self._write(f"{tab}!A1:{_last_column(lignes)}{len(lignes)}", lignes)
        logger.info("Onglet %s restaure depuis %s | %d ligne(s)", tab, backup, len(lignes))
        return len(lignes)

    def rewrite_review_rows(self, entries: list[ReviewEntry]) -> list[int]:
        """Reconstruit `21_A_VERIFIER` a partir des SEULES entrees donnees.

        L'ancienne migration marquait les doublons en base et croyait en
        avoir fini : l'onglet, lui, gardait ses lignes secondaires. On
        efface donc les lignes - jamais l'en-tete - et on les reecrit dans
        l'ordre, sans trou.

        Rend la liste des numeros de ligne attribues, dans le meme ordre
        que `entries`. L'appelant DOIT s'en servir pour recalculer les
        `review_row` stockes : apres un compactage, les positions memorisees
        ne valent plus rien.
        """
        self.ensure_review_tab()
        avant = self.clear_review_rows()
        lignes: list[int] = []
        for position, entry in enumerate(entries, start=2):
            self._write(
                f"{TAB_REVIEW}!A{position}:{REVIEW_LAST_COL}{position}",
                [build_review_row(entry)],
            )
            self._paint_review_row(position)
            lignes.append(position)

        # Le surplus est efface APRES la reecriture. Composio n'expose
        # aucune suppression de ligne : les lignes excedentaires restent
        # dans la grille mais VIDES, donc en fin d'onglet, et une lecture
        # n'en rend aucune. Aucun trou ne subsiste entre deux lignes
        # ecrites, ce qui est la propriete qui compte.
        fin = avant + 1
        derniere = len(lignes) + 1
        if fin > derniere:
            self._gw.execute(
                "GOOGLESHEETS_CLEAR_VALUES",
                {
                    "spreadsheet_id": self._sheet,
                    "range": f"{TAB_REVIEW}!A{derniere + 1}:{REVIEW_LAST_COL}{fin}",
                },
            )
        logger.info(
            "Onglet %s reconstruit : %d ligne(s) canonique(s), %d ligne(s) "
            "excedentaire(s) effacees, aucun trou intercale",
            TAB_REVIEW, len(lignes), max(0, fin - derniere),
        )
        return lignes

    def append_cancellation(
        self, tab: str, identifiant: str, motif: str, avant: str
    ) -> int:
        """Conserve l'image AVANT annulation d'une ligne.

        C'est ce qui distingue une annulation d'un effacement : sans cette
        trace, plus rien ne permettrait de reconstruire la ligne d'origine
        si la decision se revelait mauvaise.
        """
        from app.cleanup_migration import TAB_CANCELLATIONS

        return self.append_row(
            TAB_CANCELLATIONS,
            [_now_iso(), tab, identifiant, motif, avant],
            "E",
        )

    def neutralize_amounts(self, tab: str, identifiant: str, motif: str) -> int:
        """Sort les montants d'une ligne des totaux, sans supprimer la ligne.

        Toute valeur NUMERIQUE est vidée - c'est elle, et elle seule, qui
        entre dans une somme. L'identifiant, les libelles et les dates
        restent lisibles, et le motif est inscrit en fin de ligne.

        La ligne n'est jamais supprimee : une suppression decalerait toutes
        les suivantes, et les references notees ailleurs pointeraient alors
        vers la mauvaise ecriture.
        """
        if tab not in self.tabs():
            return 0
        # Les lignes sont TRIMEES avant reecriture. Sans cela, une lecture
        # A1:Z rendait des lignes de vingt-six colonnes, la mention
        # d'annulation etait ajoutee en vingt-septieme position - donc en
        # colonne AA, hors de la plage relue ensuite - et un second passage
        # ne voyait plus qu'une ligne deja annulee l'avait ete.
        lignes = _trim(self.read_tab(tab))
        touchees = 0
        for index, ligne in enumerate(lignes[1:], start=2):
            if not ligne or str(ligne[0]).strip() != identifiant:
                continue
            neutralisee: list[Any] = [ligne[0]]
            for cellule in ligne[1:]:
                neutralisee.append(
                    "" if isinstance(cellule, (int, float)) else cellule
                )
            neutralisee.append(f"ANNULEE APRES CONTROLE - {motif}")
            if len(neutralisee) > 26:
                raise PipelineError(
                    f"{tab} : ligne trop large pour porter la mention "
                    f"d'annulation ({len(neutralisee)} colonnes)."
                )
            colonne = chr(ord("A") + len(neutralisee) - 1)
            self._write(f"{tab}!A{index}:{colonne}{index}", [neutralisee])
            touchees += 1
        return touchees

    def mark_import_log(self, reference: str, statut: str) -> bool:
        """Marque une entree de `14_IMPORTS_LOG`. Ne la retire JAMAIS.

        Le journal d'import est la memoire de ce que le bot a fait. En
        retirer une ligne parce qu'elle s'est revelee fausse reviendrait a
        effacer la trace de l'erreur en meme temps que l'erreur.
        """
        from app.cleanup_migration import TAB_IMPORTS

        if TAB_IMPORTS not in self.tabs():
            return False
        lignes = self.read_tab(TAB_IMPORTS)
        marquee = False
        for index, ligne in enumerate(lignes[1:], start=2):
            if len(ligne) < 3 or str(ligne[2]).strip() != reference:
                continue
            self._write(f"{TAB_IMPORTS}!D{index}:D{index}", [[statut]])
            marquee = True
        return marquee

    def append_row(self, tab: str, values: list[Any], width: str) -> int:
        self.ensure_tab(tab)
        row_index = self.next_row(tab)
        self._write(f"{tab}!A{row_index}:{width}{row_index}", [values])
        self.mark_new_row(tab, row_index, width)
        return row_index

    def next_prefixed_id(self, tab: str, prefix: str, year: int) -> str:
        ids = [str(r[0]).strip() for r in self._read(f"{tab}!A2:A400") if r]
        return next_stable_invoice_id(ids, year, prefix)

    def append_import_log(
        self, outcome: DocumentOutcome, message: dict[str, Any], row_index: int = 0
    ) -> int:
        from datetime import datetime, timezone

        doc = outcome.document
        row = build_import_log_row(
            horodatage=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            stable_id=outcome.stable_id or outcome.doc_key[:12],
            action="A valider" if outcome.pending_review else "Cree",
            statut=(
                "Archive dans Drive / A verifier - ecriture en attente"
                if outcome.pending_review
                else f"{outcome.type_label} - {outcome.action}"
            ),
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
            avertissements=tuple(outcome.warnings),
            en_attente=outcome.pending_review,
            devise=outcome.devise or "",
        )
        index = row_index or self.next_row(TAB_IMPORTS_LOG)
        self._write(f"{TAB_IMPORTS_LOG}!A{index}:F{index}", [row])
        if not row_index:
            # row_index non nul = REECRITURE d'une ligne de journal deja
            # existante (decision prise apres validation). Une mise a jour
            # n'est pas une creation : elle ne se colore pas.
            self.mark_new_row(TAB_IMPORTS_LOG, index, "F")
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


def drive_file_id(link: str) -> str:
    """Identifiant Drive porte par un lien de fichier."""
    marker = "/d/"
    if marker not in link:
        return ""
    rest = link.split(marker, 1)[1]
    return rest.split("/", 1)[0].split("?", 1)[0]


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


def _motif_escalade(raisons: list[str]) -> str:
    """Traduit les raisons libres en UN motif normalise.

    Une chaine libre rendrait toute statistique impossible : on ne saurait
    pas regrouper deux formulations du meme probleme.
    """
    texte = " ".join(raisons).lower()
    if "incoherent" in texte:
        return llm_usage.REASON_INCOHERENT_TOTALS
    if "confiance" in texte:
        return llm_usage.REASON_LOW_CONFIDENCE
    if "illisible" in texte or "aucune lecture" in texte:
        return llm_usage.REASON_UNREADABLE_IMAGE
    if "absent" in texte:
        return llm_usage.REASON_MISSING_FIELDS
    return llm_usage.REASON_DIRECT
