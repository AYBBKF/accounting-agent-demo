"""Tests de l'agent comptable et du routage des messages texte libre.

Aucun appel reseau reel : les lectures Google Sheets sont mockees. Les
donnees de test reproduisent la structure REELLE de l'onglet
04_FACTURES_VENTES du classeur de demo, doublon et anomalie inclus.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.accounting_agent import (
    AccountingAgent,
    AccountingAgentClarification,
    AccountingAgentError,
    format_amount,
    is_revenue_question,
    parse_amount,
    parse_date,
)

TABS = [
    "00_DASHBOARD", "01_PARAMETRES", "02_CLIENTS", "03_FOURNISSEURS",
    "04_FACTURES_VENTES", "05_FACTURES_ACHATS", "06_RELEVE_BANCAIRE",
    "13_ANOMALIES", "BOT_FACTURES",
]

HEADERS = [
    "ID", "Date", "Numéro facture", "ID Client", "Client", "Description",
    "Montant HT (facture)", "Taux TVA (%)", "Montant TVA (facture)",
    "Montant TTC (facture)", "Montant TTC théorique (formule)",
    "Écart TTC (formule)", "Doublon numéro? (formule)", "Échéance",
    "Montant payé", "Statut", "Jours de retard (formule)",
]


def _row(rid, d, num, ttc, theorique=None):
    return [
        rid, d, num, "CLI-001", "Client (DEMO)", "desc",
        "0,00 MAD", "20%", "0,00 MAD", ttc, theorique or ttc,
        "0,00 MAD", "", "2026-09-01", "0,00 MAD", "Payee", "0",
    ]


SALES_ROWS = [
    HEADERS,
    _row("FV-2026-001", "2026-07-31", "FAC-VTE-2026-001", "18 477,10 MAD"),
    _row("FV-2026-002", "2026-06-11", "FAC-VTE-2026-002", "35 583,74 MAD"),
    _row("FV-2026-003", "2026-08-02", "FAC-VTE-2026-003", "43 955,13 MAD"),
    # Doublon volontaire : meme numero de facture que FV-2026-002.
    _row("FV-2026-002-DUP", "2026-06-11", "FAC-VTE-2026-002", "35 583,74 MAD"),
    # Anomalie volontaire : TTC facture != TTC theorique.
    _row("FV-2026-011", "2026-08-10", "FAC-VTE-2026-011", "1 500,00 MAD", "1 200,00 MAD"),
]

# 18477.10 + 35583.74 + 43955.13 + 1500.00 (doublon exclu)
EXPECTED_TOTAL = Decimal("99515.97")


def _agent():
    return AccountingAgent(api_key="cle-de-test", spreadsheet_id="sheet-de-test")


# --------------------------------------------------------------------------
# Parsing des montants du classeur (format francais, espaces insecables)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15 397,58 MAD", Decimal("15397.58")),
        ("15 397,58 MAD", Decimal("15397.58")),   # espace insecable
        ("1 500,00 MAD", Decimal("1500.00")),
        ("0,00 MAD", Decimal("0")),
        ("15,397.58", Decimal("15397.58")),            # format anglo-saxon
        (1234.5, Decimal("1234.5")),
    ],
)
def test_parse_amount_handles_real_sheet_formats(raw, expected):
    parsed = parse_amount(raw)
    assert parsed is not None
    assert parsed[0] == expected


@pytest.mark.parametrize("raw", ["", None, "   ", "Payee", "n/a"])
def test_parse_amount_returns_none_instead_of_inventing_a_value(raw):
    assert parse_amount(raw) is None


def test_parse_date_and_format_amount():
    assert parse_date("2026-08-10").isoformat() == "2026-08-10"
    assert parse_date("10/08/2026").isoformat() == "2026-08-10"
    assert parse_date("pas une date") is None
    assert format_amount(Decimal("308467.31"), "MAD") == "308 467,31 MAD"


@pytest.mark.parametrize(
    "question",
    [
        "donne-moi le chiffre d'affaires total TTC",
        "Chiffre d'affaires ?",
        "quel est le total TTC",
        "mes revenus",
    ],
)
def test_revenue_intent_is_detected(question):
    assert is_revenue_question(question) is True


def test_non_revenue_question_is_not_misrouted():
    assert is_revenue_question("bonjour, comment vas-tu ?") is False


# --------------------------------------------------------------------------
# Calcul du chiffre d'affaires
# --------------------------------------------------------------------------

def test_revenue_total_is_correct_and_excludes_duplicates():
    agent = _agent()
    with patch.object(AccountingAgent, "list_tabs", return_value=TABS), \
         patch.object(AccountingAgent, "read_tab", return_value=SALES_ROWS):
        result = agent.compute_revenue(chat_id=999653395)

    assert result.total_ttc == EXPECTED_TOTAL
    assert result.invoice_count == 4
    assert result.duplicates_excluded == 1
    assert result.tab == "04_FACTURES_VENTES"
    assert result.ttc_column == "Montant TTC (facture)"
    assert result.currency == "MAD"
    assert result.period_start.isoformat() == "2026-06-11"
    assert result.period_end.isoformat() == "2026-08-10"


def test_revenue_reports_ttc_mismatch_anomaly_without_hiding_it():
    agent = _agent()
    with patch.object(AccountingAgent, "list_tabs", return_value=TABS), \
         patch.object(AccountingAgent, "read_tab", return_value=SALES_ROWS):
        result = agent.compute_revenue(chat_id=1)
    assert any("FAC-VTE-2026-011" in n for n in result.notes)


def test_revenue_message_states_amount_period_and_tab():
    agent = _agent()
    with patch.object(AccountingAgent, "list_tabs", return_value=TABS), \
         patch.object(AccountingAgent, "read_tab", return_value=SALES_ROWS):
        message = agent.answer(999653395, "donne-moi le chiffre d'affaires total TTC")
    assert "99 515,97 MAD" in message
    assert "04_FACTURES_VENTES" in message
    assert "2026-06-11 -> 2026-08-10" in message


def test_sales_tab_is_chosen_over_purchases_tab():
    agent = _agent()
    assert agent.pick_sales_tab(TABS) == "04_FACTURES_VENTES"


def test_missing_sales_tab_raises_explicit_error():
    agent = _agent()
    with pytest.raises(AccountingAgentError) as exc:
        agent.pick_sales_tab(["00_DASHBOARD", "05_FACTURES_ACHATS"])
    assert "Aucun onglet" in str(exc.value)


def test_empty_tab_is_reported_explicitly_not_as_zero():
    agent = _agent()
    with patch.object(AccountingAgent, "list_tabs", return_value=TABS), \
         patch.object(AccountingAgent, "read_tab", return_value=[]):
        with pytest.raises(AccountingAgentError) as exc:
            agent.compute_revenue(chat_id=1)
    assert "vide" in str(exc.value).lower()


def test_headers_only_tab_is_reported_explicitly():
    agent = _agent()
    with patch.object(AccountingAgent, "list_tabs", return_value=TABS), \
         patch.object(AccountingAgent, "read_tab", return_value=[HEADERS]):
        with pytest.raises(AccountingAgentError) as exc:
            agent.compute_revenue(chat_id=1)
    assert "aucune ligne" in str(exc.value).lower()


def test_tab_without_ttc_column_is_reported_as_malformed():
    agent = _agent()
    rows = [["ID", "Date", "Montant HT"], ["A", "2026-01-01", "10,00 MAD"]]
    with patch.object(AccountingAgent, "list_tabs", return_value=TABS), \
         patch.object(AccountingAgent, "read_tab", return_value=rows):
        with pytest.raises(AccountingAgentError) as exc:
            agent.compute_revenue(chat_id=1)
    assert "TTC" in str(exc.value)


def test_ambiguous_ttc_columns_ask_for_clarification():
    agent = _agent()
    rows = [
        ["ID", "Numéro facture", "Date", "Montant TTC societe A", "Montant TTC societe B"],
        ["1", "F-1", "2026-01-01", "10,00 MAD", "20,00 MAD"],
    ]
    with patch.object(AccountingAgent, "list_tabs", return_value=TABS), \
         patch.object(AccountingAgent, "read_tab", return_value=rows):
        with pytest.raises(AccountingAgentClarification):
            agent.compute_revenue(chat_id=1)


def test_revenue_uses_the_client_own_composio_user_id():
    """Le classeur doit etre lu avec la connexion du client, pas une connexion
    globale : user_id doit valoir telegram_<chat_id>."""
    agent = _agent()
    seen: list[str] = []

    def fake_execute(slug, arguments, user_id):
        seen.append(user_id)
        if slug == "GOOGLESHEETS_GET_SPREADSHEET_INFO":
            return {"sheets": [{"properties": {"title": t}} for t in TABS]}
        return {"valueRanges": [{"values": SALES_ROWS}]}

    with patch.object(AccountingAgent, "_execute", side_effect=fake_execute):
        agent.compute_revenue(chat_id=424242)

    assert seen and all(u == "telegram_424242" for u in seen)


def test_composio_failure_never_leaks_the_api_key():
    agent = AccountingAgent(api_key="super-secret", spreadsheet_id="s")
    fake_client = MagicMock()
    fake_client.post.side_effect = RuntimeError("boom")
    with patch("httpx.Client", return_value=fake_client):
        with pytest.raises(AccountingAgentError) as exc:
            agent.list_tabs("telegram_1")
    assert "super-secret" not in str(exc.value)


def test_agent_not_configured_is_reported_clearly():
    agent = AccountingAgent(api_key="", spreadsheet_id="")
    assert agent.is_configured is False
    with pytest.raises(AccountingAgentError):
        agent.answer(1, "chiffre d'affaires")


def test_unknown_question_gets_a_helpful_answer_not_silence():
    agent = _agent()
    reply = agent.answer(1, "bonjour")
    assert reply.strip()
    assert "/help" in reply
