"""Tests de l'assistant comptable conversationnel.

Aucun appel reseau reel : les lectures Google Sheets sont mockees et le
routeur LLM est soit remplace par un faux, soit absent (le routeur de
secours par mots-cles prend alors le relais).

Les donnees reproduisent la structure REELLE de l'onglet 04_FACTURES_VENTES
du classeur de demo, avec son doublon (FAC-VTE-2026-002) et son anomalie
(FAC-VTE-2026-011) volontaires.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.accounting_agent import (
    AccountingAgent,
    AccountingAgentClarification,
    AccountingAgentError,
    format_amount,
    parse_amount,
    parse_date,
)
from app.agent_intent import Plan, fallback_plan

TABS = [
    "00_DASHBOARD", "01_PARAMETRES", "02_CLIENTS", "03_FOURNISSEURS",
    "04_FACTURES_VENTES", "05_FACTURES_ACHATS", "06_RELEVE_BANCAIRE",
    "08_RAPPROCHEMENT", "11_IMPAYES", "13_ANOMALIES",
]

HEADERS = [
    "ID", "Date", "Numéro facture", "ID Client", "Client", "Description",
    "Montant HT (facture)", "Taux TVA (%)", "Montant TVA (facture)",
    "Montant TTC (facture)", "Montant TTC théorique (formule)",
    "Écart TTC (formule)", "Doublon numéro? (formule)", "Échéance",
    "Montant payé", "Statut", "Jours de retard (formule)",
]


def _row(rid, d, num, client, ht, vat, ttc, theo=None, due="2026-09-01", paid=None, status="Payee"):
    return [
        rid, d, num, "CLI-001", client, "desc", ht, "20%", vat, ttc,
        theo or ttc, "0,00 MAD", "", due, paid if paid is not None else ttc, status, "0",
    ]


SALES_ROWS = [
    HEADERS,
    _row("FV-2026-001", "2026-07-31", "FAC-VTE-2026-001", "Atlas Textile SARL (DEMO)",
         "15 397,58 MAD", "3 079,52 MAD", "18 477,10 MAD"),
    _row("FV-2026-002", "2026-06-11", "FAC-VTE-2026-002", "Riad Marrakech Hotels (DEMO)",
         "29 653,12 MAD", "5 930,62 MAD", "35 583,74 MAD"),
    _row("FV-2026-003", "2026-08-02", "FAC-VTE-2026-003", "Oujda Agro Distribution (DEMO)",
         "39 959,21 MAD", "3 995,92 MAD", "43 955,13 MAD",
         paid="0,00 MAD", status="Impayee"),
    # Doublon volontaire : meme numero de facture que FV-2026-002.
    _row("FV-2026-002-DUP", "2026-06-11", "FAC-VTE-2026-002", "Riad Marrakech Hotels (DEMO)",
         "29 653,12 MAD", "5 930,62 MAD", "35 583,74 MAD"),
    # Anomalie volontaire : TTC enregistre != TTC theorique.
    _row("FV-2026-011", "2026-08-10", "FAC-VTE-2026-011", "Atlas Textile SARL (DEMO)",
         "1 000,00 MAD", "200,00 MAD", "1 500,00 MAD", theo="1 200,00 MAD",
         paid="0,00 MAD", status="Impayee"),
]

# 18477.10 + 35583.74 + 43955.13 + 1500.00 (doublon exclu)
EXPECTED_TTC = Decimal("99515.97")


def _agent(router=None):
    return AccountingAgent(api_key="cle-de-test", spreadsheet_id="sheet-de-test", router=router)


def _ask(question, agent=None, chat_id=999653395, rows=None):
    agent = agent or _agent()
    with patch.object(AccountingAgent, "list_tabs", return_value=TABS), \
         patch.object(AccountingAgent, "read_tab", return_value=SALES_ROWS if rows is None else rows):
        return agent.answer(chat_id, question)


# --------------------------------------------------------------------------
# Parsing (inchange, mais toujours garde-fou : jamais de valeur inventee)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15 397,58 MAD", Decimal("15397.58")),
        ("15 397,58 MAD", Decimal("15397.58")),   # espace insecable
        ("1 500,00 MAD", Decimal("1500.00")),
        ("15,397.58", Decimal("15397.58")),
        (1234.5, Decimal("1234.5")),
    ],
)
def test_parse_amount_handles_real_sheet_formats(raw, expected):
    parsed = parse_amount(raw)
    assert parsed is not None and parsed[0] == expected


@pytest.mark.parametrize("raw", ["", None, "   ", "Payee", "n/a"])
def test_parse_amount_returns_none_instead_of_inventing_a_value(raw):
    assert parse_amount(raw) is None


def test_parse_date_and_format_amount():
    assert parse_date("2026-08-10").isoformat() == "2026-08-10"
    assert parse_date("pas une date") is None
    assert format_amount(Decimal("308467.31"), "MAD") == "308 467,31 MAD"


# --------------------------------------------------------------------------
# Les 8 scenarios demandes
# --------------------------------------------------------------------------

def test_question_anomalie_answers_from_real_data():
    """La regression signalee : cette question renvoyait 'je sais seulement
    calculer le CA' alors que l'anomalie etait deja detectee."""
    reply = _ask("Quelle facture contient une anomalie ?")
    assert "FAC-VTE-2026-011" in reply
    assert "1 500,00 MAD" in reply      # TTC enregistre
    assert "1 200,00 MAD" in reply      # TTC theorique
    assert "300,00 MAD" in reply        # ecart
    assert "seulement" not in reply.lower()


def test_question_combien_de_factures():
    reply = _ask("Combien de factures avons-nous ?")
    assert "11 facture" in reply or "4 facture" in reply
    assert "04_FACTURES_VENTES" in reply


def test_question_ca_ttc_du_mois_d_aout_filters_the_period():
    reply = _ask("CA TTC du mois d'aout")
    # Aout seulement : 43 955,13 + 1 500,00
    assert "45 455,13 MAD" in reply
    assert "2026-08-01 -> 2026-08-31" in reply


def test_question_doublons():
    reply = _ask("Donne-moi les doublons")
    assert "FAC-VTE-2026-002" in reply
    assert "doublon" in reply.lower()


def test_question_en_darija_is_understood():
    reply = _ask("chhal men factura 3andna?")
    assert "facture" in reply.lower()
    assert "04_FACTURES_VENTES" in reply


def test_ambiguous_question_asks_a_short_question():
    with pytest.raises(AccountingAgentClarification) as exc:
        _ask("et alors ?")
    assert "?" in str(exc.value)


def test_write_request_requires_confirmation_and_writes_nothing():
    agent = _agent()
    reply = _ask("ajoute une echeance dans le calendrier pour FAC-VTE-2026-003", agent=agent)
    assert "confirm" in reply.lower()
    assert "Rien n'a encore ete ecrit" in reply
    assert agent.pending_write(999653395) is not None

    # Un 'non' annule et ne modifie rien.
    cancelled = agent.answer(999653395, "non")
    assert "annulee" in cancelled.lower()
    assert agent.pending_write(999653395) is None


def test_write_confirmation_is_scoped_to_the_asking_chat():
    agent = _agent()
    _ask("ajoute une ligne dans le sheet", agent=agent, chat_id=111)
    assert agent.pending_write(111) is not None
    # Un autre client ne doit pas heriter de la confirmation en attente.
    assert agent.pending_write(222) is None


def test_two_chats_are_isolated_by_composio_user_id():
    """Chaque client lit le classeur avec SA connexion : user_id doit valoir
    telegram_<chat_id>, jamais celui d'un autre client."""
    agent = _agent()
    seen: list[str] = []

    def fake_execute(slug, arguments, user_id):
        seen.append(user_id)
        if slug == "GOOGLESHEETS_GET_SPREADSHEET_INFO":
            return {"sheets": [{"properties": {"title": t}} for t in TABS]}
        return {"valueRanges": [{"values": SALES_ROWS}]}

    with patch.object(AccountingAgent, "_execute", side_effect=fake_execute):
        agent.answer(111111, "chiffre d'affaires TTC")
        first = list(seen)
        seen.clear()
        agent.answer(222222, "chiffre d'affaires TTC")

    assert first and all(u == "telegram_111111" for u in first)
    assert seen and all(u == "telegram_222222" for u in seen)
    assert "telegram_111111" not in seen


# --------------------------------------------------------------------------
# Autres intentions
# --------------------------------------------------------------------------

def test_revenue_ttc_total_excludes_duplicates():
    reply = _ask("donne-moi le chiffre d'affaires total TTC")
    assert format_amount(EXPECTED_TTC, "MAD") in reply
    assert "Doublons exclus : 1" in reply


def test_unpaid_invoices_are_listed_with_outstanding_amounts():
    reply = _ask("quelles factures sont impayees ?")
    assert "FAC-VTE-2026-003" in reply
    assert "43 955,13 MAD" in reply


def test_vat_collected_is_summed_from_the_vat_column():
    reply = _ask("quelle est la TVA collectee ?")
    # 3079.52 + 5930.62 + 3995.92 + 200.00
    assert "13 206,06 MAD" in reply


def test_revenue_ht_uses_the_ht_column():
    reply = _ask("donne-moi le chiffre d'affaires HT")
    # 15397.58 + 29653.12 + 39959.21 + 1000.00
    assert "86 009,91 MAD" in reply


def test_monthly_totals_are_grouped_by_month():
    reply = _ask("donne-moi les totaux par mois")
    assert "2026-06" in reply and "2026-07" in reply and "2026-08" in reply


def test_find_invoice_by_number_reports_its_anomaly():
    reply = _ask("montre-moi la facture FAC-VTE-2026-011")
    assert "FAC-VTE-2026-011" in reply
    assert "Anomalie" in reply


def test_out_of_domain_question_gets_a_clean_answer_not_silence():
    agent = _agent(router=MagicMock(plan=lambda q: Plan(intent="out_of_domain")))
    reply = agent.answer(1, "quelle est la meteo demain ?")
    assert reply.strip()
    assert "/help" in reply


# --------------------------------------------------------------------------
# Fiabilite : le LLM ne calcule jamais
# --------------------------------------------------------------------------

def test_llm_router_never_receives_amounts_and_never_supplies_totals():
    """Le routeur ne voit que la question ; le total vient du classeur."""
    captured: list[str] = []

    class SpyRouter:
        def plan(self, question):
            captured.append(question)
            # Meme si le LLM "proposait" un chiffre, il est ignore.
            return Plan(intent="revenue_ttc")

    reply = _ask("chiffre d'affaires", agent=_agent(router=SpyRouter()))
    assert captured == ["chiffre d'affaires"]
    assert format_amount(EXPECTED_TTC, "MAD") in reply


def test_router_failure_falls_back_without_breaking_the_bot():
    class BrokenRouter:
        def plan(self, question):
            raise RuntimeError("LLM indisponible")

    agent = _agent(router=BrokenRouter())
    with pytest.raises(RuntimeError):
        _ask("chiffre d'affaires", agent=agent)
    # Le fallback interne d'agent_intent couvre le cas reel (voir test_agent_intent).
    assert fallback_plan("chiffre d'affaires").intent == "revenue_ttc"


def test_unparseable_amounts_are_reported_not_guessed():
    rows = [HEADERS, _row("FV-1", "2026-01-01", "F-1", "C", "10,00 MAD", "2,00 MAD", "n/a")]
    with pytest.raises(AccountingAgentError) as exc:
        _ask("chiffre d'affaires TTC", rows=rows)
    assert "TTC" in str(exc.value)


# --------------------------------------------------------------------------
# Robustesse du classeur
# --------------------------------------------------------------------------

def test_empty_tab_is_reported_explicitly_not_as_zero():
    with pytest.raises(AccountingAgentError) as exc:
        _ask("chiffre d'affaires TTC", rows=[])
    assert "vide" in str(exc.value).lower()


def test_headers_only_tab_is_reported_explicitly():
    with pytest.raises(AccountingAgentError) as exc:
        _ask("chiffre d'affaires TTC", rows=[HEADERS])
    assert "aucune ligne" in str(exc.value).lower()


def test_tab_without_ttc_column_is_reported_as_malformed():
    rows = [["ID", "Date", "Montant HT"], ["A", "2026-01-01", "10,00 MAD"]]
    with pytest.raises(AccountingAgentError) as exc:
        _ask("chiffre d'affaires TTC", rows=rows)
    assert "TTC" in str(exc.value)


def test_ambiguous_ttc_columns_ask_for_clarification():
    rows = [
        ["ID", "Numéro facture", "Date", "Montant TTC societe A", "Montant TTC societe B"],
        ["1", "F-1", "2026-01-01", "10,00 MAD", "20,00 MAD"],
    ]
    with pytest.raises(AccountingAgentClarification):
        _ask("chiffre d'affaires TTC", rows=rows)


def test_missing_sales_tab_raises_explicit_error():
    with pytest.raises(AccountingAgentError) as exc:
        _agent().pick_sales_tab(["00_DASHBOARD", "05_FACTURES_ACHATS"])
    assert "Aucun onglet" in str(exc.value)


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
