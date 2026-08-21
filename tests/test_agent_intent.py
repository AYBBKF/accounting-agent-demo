"""Tests du routeur d'intention multilingue (francais / darija / arabe).

Le routeur ne fait que CLASSER la demande : il ne calcule rien et ne recoit
aucun montant. On verifie le routeur de secours (sans reseau) et le
comportement du routeur LLM face a une reponse valide, invalide ou absente.
"""
import json
from unittest.mock import MagicMock

import pytest

from app.agent_intent import (
    INTENTS,
    LLMIntentRouter,
    Plan,
    fallback_plan,
)


@pytest.mark.parametrize(
    "question,expected",
    [
        # Francais
        ("Quelle facture contient une anomalie ?", "anomalies"),
        ("Combien de factures avons-nous ?", "invoice_count"),
        ("donne-moi le chiffre d'affaires total TTC", "revenue_ttc"),
        ("chiffre d'affaires HT", "revenue_ht"),
        ("Donne-moi les doublons", "duplicates"),
        ("quelles factures sont impayees ?", "unpaid"),
        ("quelle est la TVA collectee ?", "vat_collected"),
        ("donne-moi les totaux par mois", "monthly_totals"),
        ("montre-moi le releve bancaire", "bank_lines"),
        ("ou en est le rapprochement ?", "reconciliation"),
        # Darija translitteree
        ("chhal men factura 3andna?", "invoice_count"),
        ("wach kayn chi ghalat f les factures?", "anomalies"),
        # Arabe
        ("كم فاتورة عندنا؟", "invoice_count"),
        ("ما هو رقم المعاملات؟", "revenue_ttc"),
    ],
)
def test_fallback_router_understands_all_three_languages(question, expected):
    assert fallback_plan(question).intent == expected


def test_fallback_router_detects_a_month_period():
    plan = fallback_plan("CA TTC du mois d'aout")
    assert plan.intent == "revenue_ttc"
    assert plan.period_start.isoformat() == "2026-08-01"
    assert plan.period_end.isoformat() == "2026-08-31"


def test_fallback_router_detects_purchases_scope():
    assert fallback_plan("total TTC des achats fournisseurs").scope == "purchases"
    assert fallback_plan("chiffre d'affaires").scope == "sales"


def test_fallback_router_extracts_an_invoice_number():
    plan = fallback_plan("montre-moi FAC-VTE-2026-011")
    assert plan.intent == "find_invoice"
    assert plan.invoice_number == "FAC-VTE-2026-011"


def test_the_word_facture_alone_is_not_mistaken_for_an_invoice_number():
    # Regression : le motif attrapait le "FAC" de "facture".
    assert fallback_plan("une facture svp").invoice_number is None


@pytest.mark.parametrize(
    "question",
    ["ajoute une ligne dans le sheet", "cree une echeance dans le calendrier",
     "supprime la facture FAC-VTE-2026-003", "modifie le montant"],
)
def test_write_requests_are_routed_to_write_action(question):
    plan = fallback_plan(question)
    assert plan.intent == "write_action"
    assert plan.write_summary


def test_ambiguous_question_is_routed_to_clarify_with_a_question():
    plan = fallback_plan("et alors ?")
    assert plan.intent == "clarify"
    assert plan.clarification_question and "?" in plan.clarification_question


def test_every_fallback_intent_is_a_known_intent():
    for q in ["chiffre d'affaires", "anomalie", "doublon", "impaye", "bonjour",
              "ajoute une ligne", "FAC-VTE-2026-001"]:
        assert fallback_plan(q).intent in INTENTS


# --------------------------------------------------------------------------
# Routeur LLM
# --------------------------------------------------------------------------

def _llm(payload, model="gpt-5.6-luna"):
    client = MagicMock()
    client.responses.create.return_value = MagicMock(output_text=json.dumps(payload))
    return LLMIntentRouter(api_key="", model=model, client=client)


def _valid_payload(**overrides):
    payload = {
        "intent": "anomalies", "scope": "sales", "period_start": None,
        "period_end": None, "client": None, "invoice_number": None,
        "clarification_question": None, "write_summary": None, "language": "fr",
    }
    payload.update(overrides)
    return payload


def test_llm_router_parses_a_valid_plan():
    router = _llm(_valid_payload(period_start="2026-08-01", period_end="2026-08-31"))
    plan = router.plan("chi ghalat f august?")
    assert plan.intent == "anomalies"
    assert plan.source == "llm"
    assert plan.period_start.isoformat() == "2026-08-01"


def test_llm_router_sends_the_question_but_no_amounts():
    client = MagicMock()
    client.responses.create.return_value = MagicMock(output_text=json.dumps(_valid_payload()))
    router = LLMIntentRouter(api_key="", model="gpt-5.6-luna", client=client)
    router.plan("Quelle facture contient une anomalie ?")

    sent = client.responses.create.call_args.kwargs["input"]
    user_turn = [m for m in sent if m["role"] == "user"]
    assert user_turn == [{"role": "user", "content": "Quelle facture contient une anomalie ?"}]
    # Aucun montant du classeur n'est transmis au LLM.
    assert "MAD" not in json.dumps(sent)


def test_llm_router_uses_the_configured_model():
    client = MagicMock()
    client.responses.create.return_value = MagicMock(output_text=json.dumps(_valid_payload()))
    LLMIntentRouter(api_key="", model="gpt-5.6-luna", client=client).plan("anomalies ?")
    assert client.responses.create.call_args.kwargs["model"] == "gpt-5.6-luna"


def test_llm_router_falls_back_when_the_call_fails():
    client = MagicMock()
    client.responses.create.side_effect = RuntimeError("indisponible")
    router = LLMIntentRouter(api_key="", model="m", client=client)
    plan = router.plan("Quelle facture contient une anomalie ?")
    assert plan.intent == "anomalies"
    assert plan.source == "fallback"


def test_llm_router_falls_back_on_an_unknown_intent():
    router = _llm(_valid_payload(intent="tout_supprimer"))
    plan = router.plan("donne-moi les doublons")
    assert plan.intent == "duplicates"
    assert plan.source == "fallback"


def test_llm_router_falls_back_on_an_empty_response():
    client = MagicMock()
    client.responses.create.return_value = MagicMock(output_text=None)
    router = LLMIntentRouter(api_key="", model="m", client=client)
    assert router.plan("les impayes").intent == "unpaid"


def test_router_without_api_key_is_not_configured_but_still_plans():
    router = LLMIntentRouter(api_key="", model="m")
    assert router.is_configured is False
    assert router.plan("donne-moi les doublons").intent == "duplicates"
