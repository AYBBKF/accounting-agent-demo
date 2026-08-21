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
from typing import Any

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

_REVENUE_INTENT_PATTERNS = (
    re.compile(r"chiffre\s+d['’ ]?affaires?", re.I),
    re.compile(r"\bca\b.*\bttc\b", re.I),
    re.compile(r"\bttc\b.*\btotal\b", re.I),
    re.compile(r"\btotal\b.*\bttc\b", re.I),
    re.compile(r"\brevenus?\b", re.I),
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


def is_revenue_question(text: str) -> bool:
    return any(p.search(text) for p in _REVENUE_INTENT_PATTERNS)


@dataclass
class RevenueResult:
    tab: str
    ttc_column: str
    total_ttc: Decimal
    currency: str
    invoice_count: int
    duplicates_excluded: int
    period_start: date | None
    period_end: date | None
    notes: list[str] = field(default_factory=list)

    def to_message(self) -> str:
        lines = [
            f"Chiffre d'affaires total TTC : {format_amount(self.total_ttc, self.currency)}",
            "",
            f"- Onglet utilise : {self.tab}",
            f"- Colonne : {self.ttc_column}",
            f"- Factures comptees : {self.invoice_count}",
        ]
        if self.period_start and self.period_end:
            lines.append(f"- Periode detectee : {self.period_start} -> {self.period_end}")
        else:
            lines.append("- Periode detectee : non determinable (colonne date absente ou vide)")
        if self.duplicates_excluded:
            lines.append(
                f"- Doublons exclus : {self.duplicates_excluded} "
                "(meme numero de facture, compte une seule fois)"
            )
        for note in self.notes:
            lines.append(f"- Attention : {note}")
        return "\n".join(lines)


class AccountingAgent:
    """Lit le classeur du client via Composio et repond a ses questions.

    `spreadsheet_id` est le classeur configure (GOOGLE_SHEET_ID). Le
    `user_id` Composio est derive du chat Telegram, donc chaque client
    interroge le classeur avec SA propre connexion Google.
    """

    def __init__(self, api_key: str, spreadsheet_id: str, timeout_seconds: float | None = None) -> None:
        self._api_key = api_key
        self._spreadsheet_id = spreadsheet_id
        self._timeout = timeout_seconds or _REQUEST_TIMEOUT_SECONDS
        self._client = None

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
        except ImportError as exc:  # pragma: no cover - dependance manquante
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
                "Impossible de joindre Google Sheets pour l'instant. Reessaie dans un moment."
            ) from exc
        if not result.get("successful", False):
            logger.warning("Outil Composio '%s' en echec pour %s", slug, user_id)
            raise AccountingAgentError(
                "Google Sheets a refuse la lecture. Verifie que Google Sheets est "
                "bien connecte avec /status, puis relance /connect si besoin."
            )
        return result.get("data") or {}

    def list_tabs(self, user_id: str) -> list[str]:
        data = self._execute(
            "GOOGLESHEETS_GET_SPREADSHEET_INFO",
            {"spreadsheet_id": self._spreadsheet_id},
            user_id,
        )
        tabs = [
            (s.get("properties") or {}).get("title", "")
            for s in data.get("sheets", [])
        ]
        return [t for t in tabs if t]

    def read_tab(self, user_id: str, tab: str) -> list[list[Any]]:
        data = self._execute(
            "GOOGLESHEETS_BATCH_GET",
            {"spreadsheet_id": self._spreadsheet_id, "ranges": [f"{tab}!A1:Z{_MAX_ROWS}"]},
            user_id,
        )
        ranges = data.get("valueRanges") or []
        if not ranges:
            return []
        return ranges[0].get("values") or []

    def pick_sales_tab(self, tabs: list[str]) -> str:
        candidates = [
            t for t in tabs
            if any(p.search(t) for p in _SALES_TAB_PATTERNS)
            and not any(p.search(t) for p in _EXCLUDED_TAB_PATTERNS)
        ]
        if not candidates:
            raise AccountingAgentError(
                "Aucun onglet de factures de vente trouve dans le classeur. "
                f"Onglets disponibles : {', '.join(tabs) if tabs else 'aucun'}."
            )
        if len(candidates) > 1:
            # Prefere un onglet explicitement "factures ventes" s'il est unique.
            strong = [t for t in candidates if _SALES_TAB_PATTERNS[0].search(t)]
            if len(strong) == 1:
                return strong[0]
            raise AccountingAgentClarification(
                "Plusieurs onglets de ventes existent : "
                f"{', '.join(candidates)}. Lequel dois-je utiliser ?"
            )
        return candidates[0]

    def _pick_ttc_column(self, headers: list[str]) -> tuple[int, str, list[str]]:
        """Retourne (index, libelle, autres colonnes TTC detectees)."""
        ttc_cols = [
            (i, h) for i, h in enumerate(headers)
            if "TTC" in _strip_accents(str(h)).upper()
        ]
        if not ttc_cols:
            raise AccountingAgentError(
                "Aucune colonne TTC trouvee dans cet onglet. "
                f"Colonnes presentes : {', '.join(str(h) for h in headers if str(h).strip())}."
            )
        # Ecarte les colonnes de controle (theorique, ecart, difference).
        primary = [
            (i, h) for i, h in ttc_cols
            if not re.search(r"theorique|ecart|difference|controle", _strip_accents(str(h)), re.I)
        ]
        others = [h for i, h in ttc_cols if (i, h) not in primary]
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
        return primary[0][0], str(primary[0][1]), [str(h) for h in others]

    @staticmethod
    def _find_column(headers: list[str], *keywords: str) -> int | None:
        for i, h in enumerate(headers):
            normalized = _strip_accents(str(h)).upper()
            if all(k.upper() in normalized for k in keywords):
                return i
        return None

    def compute_revenue(self, chat_id: int) -> RevenueResult:
        """Calcule le CA TTC a partir du classeur, sans jamais extrapoler."""
        from app.composio_connect import composio_user_id_for_chat

        user_id = composio_user_id_for_chat(chat_id)
        tabs = self.list_tabs(user_id)
        tab = self.pick_sales_tab(tabs)
        rows = self.read_tab(user_id, tab)
        if not rows:
            raise AccountingAgentError(
                f"L'onglet '{tab}' est vide : aucune facture a additionner."
            )
        headers = [str(h) for h in rows[0]]
        data_rows = [r for r in rows[1:] if any(str(c).strip() for c in r)]
        if not data_rows:
            raise AccountingAgentError(
                f"L'onglet '{tab}' ne contient que des en-tetes, aucune ligne de facture."
            )

        ttc_index, ttc_label, other_ttc = self._pick_ttc_column(headers)
        number_index = self._find_column(headers, "NUMERO")
        if number_index is None:
            number_index = self._find_column(headers, "ID")
        date_index = self._find_column(headers, "DATE")
        theoretical_index = self._find_column(headers, "TTC", "THEORIQUE")

        total = Decimal("0")
        currency = ""
        seen_keys: set[str] = set()
        counted = 0
        duplicates = 0
        unparsed: list[str] = []
        mismatches: list[str] = []
        dates: list[date] = []

        for row_number, row in enumerate(data_rows, start=2):
            def cell(idx: int | None) -> Any:
                if idx is None or idx >= len(row):
                    return None
                return row[idx]

            key_raw = cell(number_index)
            key = str(key_raw).strip() if key_raw is not None else ""
            if key:
                if key in seen_keys:
                    duplicates += 1
                    continue
                seen_keys.add(key)

            parsed = parse_amount(cell(ttc_index))
            if parsed is None:
                unparsed.append(f"ligne {row_number}")
                continue
            amount, row_currency = parsed
            if row_currency and not currency:
                currency = row_currency
            total += amount
            counted += 1

            if theoretical_index is not None:
                theoretical = parse_amount(cell(theoretical_index))
                if theoretical is not None and theoretical[0] != amount:
                    label = key or f"ligne {row_number}"
                    mismatches.append(
                        f"{label} ({format_amount(amount, currency)} facture "
                        f"vs {format_amount(theoretical[0], currency)} theorique)"
                    )

            parsed_date = parse_date(cell(date_index))
            if parsed_date:
                dates.append(parsed_date)

        if counted == 0:
            raise AccountingAgentError(
                f"Aucun montant TTC exploitable dans l'onglet '{tab}' "
                f"(colonne '{ttc_label}'). Le classeur est-il correctement rempli ?"
            )

        notes: list[str] = []
        if mismatches:
            notes.append(
                "ecart entre TTC facture et TTC theorique sur "
                f"{len(mismatches)} facture(s) : {', '.join(mismatches)}. "
                "Le total ci-dessus utilise le TTC facture."
            )
        if other_ttc:
            notes.append(
                "une autre colonne TTC existe dans l'onglet "
                f"({', '.join(other_ttc)}) ; dis-le-moi si tu veux le total sur celle-la."
            )
        if unparsed:
            notes.append(
                f"{len(unparsed)} ligne(s) ignoree(s), montant TTC illisible : {', '.join(unparsed)}."
            )

        return RevenueResult(
            tab=tab,
            ttc_column=ttc_label,
            total_ttc=total,
            currency=currency,
            invoice_count=counted,
            duplicates_excluded=duplicates,
            period_start=min(dates) if dates else None,
            period_end=max(dates) if dates else None,
            notes=notes,
        )

    def answer(self, chat_id: int, question: str) -> str:
        """Point d'entree unique du handler texte libre."""
        if not self.is_configured:
            raise AccountingAgentError(
                "Analyse du classeur non configuree sur ce bot "
                "(COMPOSIO_API_KEY ou GOOGLE_SHEET_ID manquant)."
            )
        if is_revenue_question(question):
            return self.compute_revenue(chat_id).to_message()
        return (
            "Je peux interroger ton Google Sheet connecte. Pour l'instant je sais "
            "calculer le chiffre d'affaires TTC a partir de tes factures de vente.\n"
            "Essaie : \"donne-moi le chiffre d'affaires total TTC\".\n"
            "Commandes disponibles : /help"
        )
