"""Agent comptable en langage naturel, adosse au Google Sheet du client.

Repond aux questions texte libre du bot Telegram (ex. "donne-moi le chiffre
d'affaires total TTC") en lisant REELLEMENT le classeur Google Sheets du
client via sa propre connexion Composio (user_id = "telegram_<chat_id>",
cf. app/composio_connect.py) : chaque client n'accede qu'a ses propres
donnees.

Principe de conception central : AUCUN chiffre n'est produit par un modele
de langage. Les montants sont lus dans le classeur puis additionnes par ce
module, en Decimal. Si une donnee manque ou est ambigue, l'agent le dit et
demande une precision - il n'invente jamais de valeur.

Aucun secret (cle API Composio) n'est journalise ni renvoye dans un message
d'erreur.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.agent_intent import Plan

logger = logging.getLogger("demo_bot.accounting_agent")

COMPOSIO_BASE_URL = "https://backend.composio.dev"
COMPOSIO_TOOLS_EXECUTE_PATH = "/api/v3.1/tools/execute/{tool_slug}"
_REQUEST_TIMEOUT_SECONDS = 30.0

# Nombre max de lignes lues par onglet (le classeur de demo tient tres large
# dans cette fenetre ; borne explicite pour ne jamais lire sans limite).
_MAX_ROWS = 500

# Onglets candidats pour le chiffre d'affaires (ventes). Les achats sont
# volontairement exclus : le CA, ce sont les ventes.
_SALES_TAB_PATTERNS = (
    re.compile(r"factures?_?ventes?", re.I),
    re.compile(r"\bventes?\b", re.I),
    re.compile(r"\bsales\b", re.I),
)
_EXCLUDED_TAB_PATTERNS = (
    re.compile(r"achats?", re.I),
    re.compile(r"fournisseur", re.I),
    re.compile(r"dashboard|parametre|guide|log\b", re.I),
)


class AccountingAgentError(RuntimeError):
    """Erreur metier destinee a etre montree au client (jamais de secret)."""


class AccountingAgentClarification(AccountingAgentError):
    """L'agent a besoin d'une precision avant de pouvoir repondre."""


def _strip_accents(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn"
    )


def parse_amount(raw: Any) -> tuple[Decimal, str] | None:
    """Parse un montant tel qu'il apparait dans le Sheet.

    Gere le format francais du classeur ("15 397,58 MAD", espaces insecables
    inclus) et le format anglo-saxon ("15,397.58"). Retourne (montant, devise)
    ou None si la cellule ne contient pas un montant exploitable - on ne
    devine JAMAIS une valeur.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw)), ""
    text = str(raw).strip()
    if not text:
        return None
    # Devise = lettres residuelles (MAD, EUR, DH...) ou symbole.
    currency_match = re.search(r"[A-Za-z]{2,4}|€|\$|£", text)
    currency = currency_match.group(0).upper() if currency_match else ""
    # Retire tout sauf chiffres, separateurs et signe.
    cleaned = re.sub(r"[^\d,.\-]", "", text)
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        # Le dernier separateur rencontre est le separateur decimal.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Virgule decimale francaise (une seule virgule, <=2 decimales).
        if re.search(r",\d{1,2}$", cleaned):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return Decimal(cleaned), currency
    except InvalidOperation:
        return None


def parse_date(raw: Any) -> date | None:
    if raw is None:
        return None
    text = str(raw).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{2})[/-](\d{2})[/-](\d{4})", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def format_amount(value: Decimal, currency: str = "") -> str:
    quantized = value.quantize(Decimal("0.01"))
    integer, _, decimals = f"{quantized:.2f}".partition(".")
    sign = ""
    if integer.startswith("-"):
        sign, integer = "-", integer[1:]
    groups = []
    while len(integer) > 3:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    groups.insert(0, integer)
    formatted = f"{sign}{' '.join(groups)},{decimals}"
    return f"{formatted} {currency}".strip()


# --------------------------------------------------------------------------
# Modele de ligne de facture, commun aux ventes et aux achats
# (les deux onglets partagent la meme structure de colonnes).
# --------------------------------------------------------------------------

@dataclass
class InvoiceRow:
    row_number: int
    key: str
    date: date | None
    third_party: str
    ht: Decimal | None
    vat: Decimal | None
    ttc: Decimal | None
    ttc_theoretical: Decimal | None
    due_date: date | None
    paid: Decimal | None
    status: str

    @property
    def gap(self) -> Decimal | None:
        if self.ttc is None or self.ttc_theoretical is None:
            return None
        return self.ttc - self.ttc_theoretical

    @property
    def has_anomaly(self) -> bool:
        gap = self.gap
        return gap is not None and gap != 0

    @property
    def outstanding(self) -> Decimal | None:
        if self.ttc is None:
            return None
        return self.ttc - (self.paid or Decimal("0"))


@dataclass
class InvoiceTable:
    tab: str
    currency: str
    rows: list[InvoiceRow]              # dedupliquees (1re occurrence gardee)
    duplicates: list[InvoiceRow]        # occurrences suivantes, exclues des totaux
    unparsed: list[str]
    ttc_column: str
    other_ttc_columns: list[str]

    def filtered(self, plan: "Plan") -> list[InvoiceRow]:
        rows = self.rows
        if plan.period_start:
            rows = [r for r in rows if r.date and r.date >= plan.period_start]
        if plan.period_end:
            rows = [r for r in rows if r.date and r.date <= plan.period_end]
        if plan.client:
            needle = _strip_accents(plan.client).lower()
            rows = [r for r in rows if needle in _strip_accents(r.third_party).lower()]
        return rows

    def period_of(self, rows: list[InvoiceRow]) -> tuple[date | None, date | None]:
        dates = [r.date for r in rows if r.date]
        return (min(dates), max(dates)) if dates else (None, None)


def _period_label(start: date | None, end: date | None) -> str:
    if start and end:
        return f"{start} -> {end}"
    return "non determinable (colonne date absente ou vide)"


class AccountingAgent:
    """Assistant comptable conversationnel adosse au classeur du client.

    Le LLM (via `router`) comprend la demande ; TOUS les montants sont lus
    dans le classeur et calcules ici en Decimal. Le LLM ne recoit aucun
    montant et n'additionne jamais rien.

    Chaque appel Composio utilise `user_id = telegram_<chat_id>` : un client
    ne peut jamais lire les donnees d'un autre.
    """

    def __init__(
        self,
        api_key: str,
        spreadsheet_id: str,
        timeout_seconds: float | None = None,
        router: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._spreadsheet_id = spreadsheet_id
        self._timeout = timeout_seconds or _REQUEST_TIMEOUT_SECONDS
        self._client = None
        self._router = router
        # Ecritures en attente de confirmation, par chat.
        self._pending_writes: dict[int, str] = {}

    # -- infrastructure -----------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key) and bool(self._spreadsheet_id)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.is_configured:
            raise AccountingAgentError(
                "Analyse du classeur non configuree sur ce bot "
                "(COMPOSIO_API_KEY ou GOOGLE_SHEET_ID manquant)."
            )
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise AccountingAgentError(f"Dependance httpx manquante: {exc}") from exc
        self._client = httpx.Client(
            base_url=COMPOSIO_BASE_URL,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        return self._client

    def _execute(self, slug: str, arguments: dict[str, Any], user_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        path = COMPOSIO_TOOLS_EXECUTE_PATH.format(tool_slug=slug)
        try:
            response = client.post(path, json={"arguments": arguments, "user_id": user_id})
            response.raise_for_status()
            result = response.json()
        except Exception as exc:  # noqa: BLE001 - jamais de secret dans le message
            logger.warning("Outil Composio '%s' injoignable pour %s: %s", slug, user_id, exc)
            raise AccountingAgentError(
                "Impossible de joindre Google pour l'instant. Reessaie dans un moment."
            ) from exc
        if not result.get("successful", False):
            logger.warning("Outil Composio '%s' en echec pour %s", slug, user_id)
            raise AccountingAgentError(
                "Google a refuse la lecture. Verifie tes connexions avec /status, "
                "puis relance /connect si besoin."
            )
        return result.get("data") or {}

    def list_tabs(self, user_id: str) -> list[str]:
        data = self._execute(
            "GOOGLESHEETS_GET_SPREADSHEET_INFO",
            {"spreadsheet_id": self._spreadsheet_id},
            user_id,
        )
        return [
            t for t in (
                (s.get("properties") or {}).get("title", "") for s in data.get("sheets", [])
            ) if t
        ]

    def read_tab(self, user_id: str, tab: str) -> list[list[Any]]:
        data = self._execute(
            "GOOGLESHEETS_BATCH_GET",
            {"spreadsheet_id": self._spreadsheet_id, "ranges": [f"{tab}!A1:Z{_MAX_ROWS}"]},
            user_id,
        )
        ranges = data.get("valueRanges") or []
        return (ranges[0].get("values") or []) if ranges else []

    # -- selection d'onglet -------------------------------------------------

    def pick_sales_tab(self, tabs: list[str]) -> str:
        return self._pick_tab(tabs, _SALES_TAB_PATTERNS, _EXCLUDED_TAB_PATTERNS, "de factures de vente")

    def pick_purchases_tab(self, tabs: list[str]) -> str:
        patterns = (re.compile(r"factures?_?achats?", re.I), re.compile(r"\bachats?\b", re.I))
        excluded = (re.compile(r"ventes?", re.I), re.compile(r"dashboard|parametre|guide|log\b", re.I))
        return self._pick_tab(tabs, patterns, excluded, "de factures d'achat")

    def _pick_tab(self, tabs, patterns, excluded, label) -> str:
        candidates = [
            t for t in tabs
            if any(p.search(t) for p in patterns) and not any(p.search(t) for p in excluded)
        ]
        if not candidates:
            raise AccountingAgentError(
                f"Aucun onglet {label} trouve dans le classeur. "
                f"Onglets disponibles : {', '.join(tabs) if tabs else 'aucun'}."
            )
        if len(candidates) > 1:
            strong = [t for t in candidates if patterns[0].search(t)]
            if len(strong) == 1:
                return strong[0]
            raise AccountingAgentClarification(
                f"Plusieurs onglets correspondent ({', '.join(candidates)}). Lequel dois-je utiliser ?"
            )
        return candidates[0]

    def _find_tab(self, tabs: list[str], pattern: str) -> str | None:
        rx = re.compile(pattern, re.I)
        for t in tabs:
            if rx.search(t):
                return t
        return None

    # -- colonnes -----------------------------------------------------------

    def _pick_ttc_column(self, headers: list[str]) -> tuple[int, str, list[str]]:
        ttc_cols = [(i, h) for i, h in enumerate(headers) if "TTC" in _strip_accents(str(h)).upper()]
        if not ttc_cols:
            raise AccountingAgentError(
                "Aucune colonne TTC trouvee dans cet onglet. "
                f"Colonnes presentes : {', '.join(str(h) for h in headers if str(h).strip())}."
            )
        primary = [
            (i, h) for i, h in ttc_cols
            if not re.search(r"theorique|ecart|difference|controle", _strip_accents(str(h)), re.I)
        ]
        others = [str(h) for i, h in ttc_cols if (i, h) not in primary]
        if not primary:
            raise AccountingAgentClarification(
                "Je ne trouve que des colonnes TTC de controle "
                f"({', '.join(str(h) for _, h in ttc_cols)}). Laquelle represente le TTC facture ?"
            )
        if len(primary) > 1:
            raise AccountingAgentClarification(
                "Plusieurs colonnes TTC facturables existent : "
                f"{', '.join(str(h) for _, h in primary)}. Laquelle dois-je additionner ?"
            )
        return primary[0][0], str(primary[0][1]), others

    @staticmethod
    def _find_column(headers: list[str], *keywords: str, exclude: str | None = None) -> int | None:
        for i, h in enumerate(headers):
            normalized = _strip_accents(str(h)).upper()
            if exclude and re.search(exclude, normalized, re.I):
                continue
            if all(k.upper() in normalized for k in keywords):
                return i
        return None

    # -- chargement des factures -------------------------------------------

    def load_invoices(self, user_id: str, scope: str = "sales") -> InvoiceTable:
        tabs = self.list_tabs(user_id)
        tab = self.pick_purchases_tab(tabs) if scope == "purchases" else self.pick_sales_tab(tabs)
        rows = self.read_tab(user_id, tab)
        if not rows:
            raise AccountingAgentError(f"L'onglet '{tab}' est vide : aucune facture a analyser.")
        headers = [str(h) for h in rows[0]]
        data_rows = [r for r in rows[1:] if any(str(c).strip() for c in r)]
        if not data_rows:
            raise AccountingAgentError(
                f"L'onglet '{tab}' ne contient que des en-tetes, aucune ligne de facture."
            )

        ttc_i, ttc_label, other_ttc = self._pick_ttc_column(headers)
        num_i = self._find_column(headers, "NUMERO") or self._find_column(headers, "ID")
        date_i = self._find_column(headers, "DATE", exclude=r"ECHEANCE|VALEUR")
        due_i = self._find_column(headers, "ECHEANCE")
        ht_i = self._find_column(headers, "HT")
        vat_i = self._find_column(headers, "TVA", exclude=r"TAUX")
        theo_i = self._find_column(headers, "TTC", "THEORIQUE")
        paid_i = self._find_column(headers, "PAYE")
        status_i = self._find_column(headers, "STATUT")
        party_i = self._find_column(headers, "CLIENT", exclude=r"^ID") \
            or self._find_column(headers, "FOURNISSEUR", exclude=r"^ID")

        def cell(row, idx):
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        def num(row, idx):
            parsed = parse_amount(cell(row, idx))
            return parsed[0] if parsed else None

        currency = ""
        seen: set[str] = set()
        kept: list[InvoiceRow] = []
        dups: list[InvoiceRow] = []
        unparsed: list[str] = []

        for line, row in enumerate(data_rows, start=2):
            parsed_ttc = parse_amount(cell(row, ttc_i))
            if parsed_ttc is None:
                unparsed.append(f"ligne {line}")
                continue
            if parsed_ttc[1] and not currency:
                currency = parsed_ttc[1]
            key_raw = cell(row, num_i)
            key = str(key_raw).strip() if key_raw is not None else ""
            party_raw = cell(row, party_i)
            invoice = InvoiceRow(
                row_number=line,
                key=key or f"ligne {line}",
                date=parse_date(cell(row, date_i)),
                third_party=str(party_raw).strip() if party_raw else "",
                ht=num(row, ht_i),
                vat=num(row, vat_i),
                ttc=parsed_ttc[0],
                ttc_theoretical=num(row, theo_i),
                due_date=parse_date(cell(row, due_i)),
                paid=num(row, paid_i),
                status=str(cell(row, status_i) or "").strip(),
            )
            if key and key in seen:
                dups.append(invoice)
                continue
            if key:
                seen.add(key)
            kept.append(invoice)

        if not kept:
            raise AccountingAgentError(
                f"Aucun montant TTC exploitable dans l'onglet '{tab}' (colonne '{ttc_label}')."
            )
        return InvoiceTable(
            tab=tab, currency=currency, rows=kept, duplicates=dups,
            unparsed=unparsed, ttc_column=ttc_label, other_ttc_columns=other_ttc,
        )

    # -- executeurs deterministes (aucun LLM ici) ---------------------------

    def _sum(self, rows: list[InvoiceRow], field: str) -> Decimal:
        total = Decimal("0")
        for r in rows:
            value = getattr(r, field)
            if value is not None:
                total += value
        return total

    def _footer(self, table: InvoiceTable, rows: list[InvoiceRow], plan: "Plan") -> list[str]:
        start, end = table.period_of(rows)
        lines = [
            f"- Onglet : {table.tab}",
            f"- Periode : {_period_label(plan.period_start or start, plan.period_end or end)}",
        ]
        if table.duplicates:
            lines.append(
                f"- Doublons exclus : {len(table.duplicates)} "
                "(meme numero de facture, compte une seule fois)"
            )
        if table.unparsed:
            lines.append(
                f"- Lignes ignorees (montant illisible) : {len(table.unparsed)} "
                f"({', '.join(table.unparsed)})"
            )
        return lines

    def _amount_answer(self, table, rows, plan, title, field, column) -> str:
        total = self._sum(rows, field)
        lines = [
            f"{title} : {format_amount(total, table.currency)}",
            "",
            f"- Colonne : {column}",
            f"- Factures comptees : {len(rows)}",
        ]
        lines.extend(self._footer(table, rows, plan))
        missing = [r.key for r in rows if getattr(r, field) is None]
        if missing:
            lines.append(
                f"- Donnee manquante sur {len(missing)} facture(s) "
                f"({', '.join(missing[:5])}) : non comptee(s)."
            )
        return "\n".join(lines)

    def _run_anomalies(self, table: InvoiceTable, rows, plan) -> str:
        flagged = [r for r in rows if r.has_anomaly]
        if not flagged:
            return (
                "Aucune anomalie de calcul detectee : sur les "
                f"{len(rows)} facture(s) de l'onglet {table.tab}, le TTC enregistre "
                "correspond au TTC theorique (HT + TVA)."
            )
        lines = []
        for r in flagged:
            lines.append(
                f"{r.key} presente une anomalie : TTC enregistre "
                f"{format_amount(r.ttc, table.currency)}, TTC theorique "
                f"{format_amount(r.ttc_theoretical, table.currency)}, soit un ecart de "
                f"{format_amount(abs(r.gap), table.currency)}."
            )
        header = (
            f"{len(flagged)} facture(s) presentent une anomalie de calcul :"
            if len(flagged) > 1 else "1 facture presente une anomalie de calcul :"
        )
        return "\n".join([header, ""] + lines + [""] + self._footer(table, rows, plan))

    def _run_duplicates(self, table: InvoiceTable, rows, plan) -> str:
        if not table.duplicates:
            return (
                f"Aucun doublon detecte dans l'onglet {table.tab} : les "
                f"{len(rows)} numeros de facture sont tous uniques."
            )
        lines = [
            f"{d.key} apparait plusieurs fois "
            f"({format_amount(d.ttc, table.currency)}, ligne {d.row_number}) "
            "- compte une seule fois dans les totaux."
            for d in table.duplicates
        ]
        header = f"{len(table.duplicates)} doublon(s) detecte(s) :"
        return "\n".join([header, ""] + lines + [""] + self._footer(table, rows, plan))

    def _run_unpaid(self, table: InvoiceTable, rows, plan) -> str:
        unpaid = [
            r for r in rows
            if (r.outstanding is not None and r.outstanding > 0)
            or re.search(r"impay|partiel|retard", _strip_accents(r.status), re.I)
        ]
        if not unpaid:
            return f"Aucune facture impayee : tout est encaisse sur l'onglet {table.tab}."
        total = self._sum(unpaid, "outstanding")
        lines = [
            f"{len(unpaid)} facture(s) impayee(s) ou partiellement payee(s), "
            f"reste a encaisser {format_amount(total, table.currency)} :",
            "",
        ]
        for r in unpaid:
            due = f", echeance {r.due_date}" if r.due_date else ""
            lines.append(
                f"- {r.key} ({r.third_party}) : reste "
                f"{format_amount(r.outstanding or Decimal('0'), table.currency)}"
                f"{due} - {r.status or 'statut non precise'}"
            )
        return "\n".join(lines + [""] + self._footer(table, rows, plan))

    def _run_monthly(self, table: InvoiceTable, rows, plan) -> str:
        buckets: dict[str, Decimal] = {}
        undated = 0
        for r in rows:
            if not r.date or r.ttc is None:
                undated += 1
                continue
            buckets.setdefault(f"{r.date.year}-{r.date.month:02d}", Decimal("0"))
            buckets[f"{r.date.year}-{r.date.month:02d}"] += r.ttc
        if not buckets:
            return f"Aucune facture datee dans l'onglet {table.tab} : total mensuel impossible."
        lines = [f"Totaux TTC par mois (onglet {table.tab}) :", ""]
        for month in sorted(buckets):
            lines.append(f"- {month} : {format_amount(buckets[month], table.currency)}")
        lines.append("")
        lines.append(f"Total : {format_amount(sum(buckets.values(), Decimal('0')), table.currency)}")
        if undated:
            lines.append(f"- {undated} facture(s) sans date exploitable, non reparties.")
        return "\n".join(lines + self._footer(table, rows, plan))

    def _run_find_invoice(self, table: InvoiceTable, rows, plan) -> str:
        needle = (plan.invoice_number or "").strip().upper()
        if not needle:
            raise AccountingAgentClarification("Quel numero de facture cherches-tu ?")
        matches = [r for r in table.rows + table.duplicates if needle in r.key.upper()]
        if not matches:
            return (
                f"Aucune facture ne correspond a '{plan.invoice_number}' dans l'onglet "
                f"{table.tab} ({len(table.rows)} factures examinees)."
            )
        lines = []
        for r in matches:
            lines.append(f"{r.key} - {r.third_party or 'tiers non precise'}")
            lines.append(f"- Date : {r.date or 'non precisee'}")
            if r.ht is not None:
                lines.append(f"- HT : {format_amount(r.ht, table.currency)}")
            if r.vat is not None:
                lines.append(f"- TVA : {format_amount(r.vat, table.currency)}")
            lines.append(f"- TTC : {format_amount(r.ttc, table.currency)}")
            if r.has_anomaly:
                lines.append(
                    f"- Anomalie : TTC theorique "
                    f"{format_amount(r.ttc_theoretical, table.currency)}, ecart "
                    f"{format_amount(abs(r.gap), table.currency)}"
                )
            lines.append(f"- Statut : {r.status or 'non precise'}")
            lines.append("")
        return "\n".join(lines + [f"- Onglet : {table.tab}"])

    def _run_list(self, table: InvoiceTable, rows, plan) -> str:
        if not rows:
            return (
                "Aucune facture ne correspond a ces criteres "
                f"(onglet {table.tab}, periode {_period_label(plan.period_start, plan.period_end)})."
            )
        shown = rows[:15]
        lines = [f"{len(rows)} facture(s) trouvee(s) :", ""]
        for r in shown:
            lines.append(
                f"- {r.key} | {r.date or 'sans date'} | {r.third_party or '-'} | "
                f"{format_amount(r.ttc, table.currency)} | {r.status or '-'}"
            )
        if len(rows) > len(shown):
            lines.append(f"... et {len(rows) - len(shown)} autre(s).")
        lines.append("")
        lines.append(f"Total TTC : {format_amount(self._sum(rows, 'ttc'), table.currency)}")
        return "\n".join(lines + self._footer(table, rows, plan))

    def _run_bank_lines(self, user_id: str) -> str:
        tabs = self.list_tabs(user_id)
        tab = self._find_tab(tabs, r"releve|bancaire|banque|bank")
        if not tab:
            raise AccountingAgentError(
                f"Aucun onglet de releve bancaire trouve. Onglets : {', '.join(tabs)}."
            )
        rows = self.read_tab(user_id, tab)
        data = [r for r in rows[1:] if any(str(c).strip() for c in r)] if rows else []
        if not data:
            return f"L'onglet {tab} ne contient aucune ligne bancaire."
        headers = [str(h) for h in rows[0]]
        debit_i = self._find_column(headers, "DEBIT")
        credit_i = self._find_column(headers, "CREDIT")
        debit = credit = Decimal("0")
        for r in data:
            for idx, acc in ((debit_i, "d"), (credit_i, "c")):
                if idx is not None and idx < len(r):
                    parsed = parse_amount(r[idx])
                    if parsed:
                        if acc == "d":
                            debit += parsed[0]
                        else:
                            credit += parsed[0]
        return "\n".join([
            f"Releve bancaire : {len(data)} ligne(s) dans l'onglet {tab}.",
            f"- Total debit : {format_amount(debit)}",
            f"- Total credit : {format_amount(credit)}",
        ])

    def _run_reconciliation(self, user_id: str) -> str:
        tabs = self.list_tabs(user_id)
        tab = self._find_tab(tabs, r"rapprochement|lettrage|reconcil")
        if not tab:
            raise AccountingAgentError(
                f"Aucun onglet de rapprochement trouve. Onglets : {', '.join(tabs)}."
            )
        rows = self.read_tab(user_id, tab)
        data = [r for r in rows[1:] if any(str(c).strip() for c in r)] if rows else []
        if not data:
            return f"L'onglet {tab} ne contient aucune ligne de rapprochement."
        headers = [str(h) for h in rows[0]]
        status_i = self._find_column(headers, "STATUT")
        counts: dict[str, int] = {}
        for r in data:
            value = str(r[status_i]).strip() if status_i is not None and status_i < len(r) else ""
            counts[value or "non precise"] = counts.get(value or "non precise", 0) + 1
        lines = [f"Rapprochement ({len(data)} ligne(s), onglet {tab}) :", ""]
        for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {status} : {n}")
        return "\n".join(lines)

    # -- ecritures : confirmation obligatoire -------------------------------

    _CONFIRM_WORDS = ("oui", "ok", "confirme", "confirmer", "vas-y", "yes", "naam", "n3am", "iyeh", "نعم", "موافق")
    _CANCEL_WORDS = ("non", "annule", "annuler", "stop", "no", "la", "لا")

    def pending_write(self, chat_id: int) -> str | None:
        return self._pending_writes.get(chat_id)

    def _request_confirmation(self, chat_id: int, summary: str) -> str:
        self._pending_writes[chat_id] = summary
        return "\n".join([
            "Cette demande modifie tes donnees. Rien n'a encore ete ecrit.",
            "",
            f"Action demandee : {summary}",
            "",
            "Reponds 'oui' pour confirmer, ou 'non' pour annuler.",
        ])

    def _resolve_confirmation(self, chat_id: int, question: str) -> str | None:
        pending = self._pending_writes.get(chat_id)
        if not pending:
            return None
        answer = _strip_accents(question).strip().lower().rstrip("!. ")
        if any(answer == w or answer.startswith(w + " ") for w in self._CANCEL_WORDS):
            del self._pending_writes[chat_id]
            return f"Action annulee, rien n'a ete modifie : {pending}"
        if any(answer == w or answer.startswith(w + " ") for w in self._CONFIRM_WORDS):
            del self._pending_writes[chat_id]
            logger.info("Ecriture confirmee (chat=%s)", chat_id)
            return (
                f"Confirmation recue pour : {pending}\n\n"
                "L'execution des ecritures (Sheets / Drive / Calendar) n'est pas "
                "encore activee sur cette demo : rien n'a ete modifie. "
                "Les lectures et analyses restent disponibles."
            )
        return None

    # -- point d'entree -----------------------------------------------------

    def answer(self, chat_id: int, question: str) -> str:
        from app.composio_connect import composio_user_id_for_chat

        if not self.is_configured:
            raise AccountingAgentError(
                "Analyse du classeur non configuree sur ce bot "
                "(COMPOSIO_API_KEY ou GOOGLE_SHEET_ID manquant)."
            )

        # Une confirmation en attente a toujours la priorite.
        resolved = self._resolve_confirmation(chat_id, question)
        if resolved is not None:
            return resolved

        plan = self._plan(question)
        logger.info(
            "Plan (chat=%s): intent=%s scope=%s source=%s langue=%s",
            chat_id, plan.intent, plan.scope, plan.source, plan.language,
        )

        if plan.intent == "clarify":
            raise AccountingAgentClarification(
                plan.clarification_question
                or "Peux-tu preciser ta demande ? (chiffre d'affaires, impayes, doublons, anomalies...)"
            )
        if plan.intent == "out_of_domain":
            return (
                "Je suis ton assistant comptable : je reponds sur tes factures, ta TVA, "
                "tes impayes, tes anomalies et ton relevé bancaire, a partir de ton "
                "Google Sheet connecte.\n"
                "Exemples : \"chiffre d'affaires TTC du mois d'aout\", "
                "\"quelle facture contient une anomalie ?\", \"donne-moi les doublons\".\n"
                "Commandes : /help"
            )
        if plan.intent == "write_action":
            return self._request_confirmation(
                chat_id, plan.write_summary or question.strip()
            )

        user_id = composio_user_id_for_chat(chat_id)

        if plan.intent == "bank_lines":
            return self._run_bank_lines(user_id)
        if plan.intent == "reconciliation":
            return self._run_reconciliation(user_id)

        table = self.load_invoices(user_id, plan.scope)
        rows = table.filtered(plan)

        if plan.intent == "anomalies":
            return self._run_anomalies(table, rows, plan)
        if plan.intent == "duplicates":
            return self._run_duplicates(table, rows, plan)
        if plan.intent == "unpaid":
            return self._run_unpaid(table, rows, plan)
        if plan.intent == "monthly_totals":
            return self._run_monthly(table, rows, plan)
        if plan.intent == "find_invoice":
            return self._run_find_invoice(table, rows, plan)
        if plan.intent == "list_invoices":
            return self._run_list(table, rows, plan)
        if plan.intent == "invoice_count":
            start, end = table.period_of(rows)
            lines = [
                f"{len(rows)} facture(s) dans l'onglet {table.tab}.",
                "",
                f"- Total TTC : {format_amount(self._sum(rows, 'ttc'), table.currency)}",
            ]
            return "\n".join(lines + self._footer(table, rows, plan))
        if plan.intent == "revenue_ht":
            label = "Total HT des achats" if plan.scope == "purchases" else "Chiffre d'affaires HT"
            return self._amount_answer(table, rows, plan, label, "ht", "Montant HT (facture)")
        if plan.intent == "vat_collected":
            return self._amount_answer(table, rows, plan, "TVA collectee", "vat", "Montant TVA (facture)")
        if plan.intent == "vat_deductible":
            return self._amount_answer(table, rows, plan, "TVA deductible", "vat", "Montant TVA (facture)")

        label = "Total TTC des achats" if plan.scope == "purchases" else "Chiffre d'affaires total TTC"
        return self._amount_answer(table, rows, plan, label, "ttc", table.ttc_column)

    def _plan(self, question: str):
        from app.agent_intent import fallback_plan

        if self._router is not None:
            return self._router.plan(question)
        return fallback_plan(question)
