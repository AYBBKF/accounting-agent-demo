"""Tests du ROUTAGE des updates Telegram (aiogram).

Regression couverte : avant ce correctif, aucun handler n'etait enregistre
pour les messages texte non-commande. aiogram journalisait
"Update ... is not handled. Duration 0 ms" et le bot restait totalement
muet face a une question comme "donne-moi le chiffre d'affaires total TTC".

On verifie ici, sans reseau, que :
  - un handler texte libre existe ET est enregistre APRES les commandes ;
  - une commande continue d'etre routee vers son handler de commande ;
  - un message texte declenche l'agent comptable ;
  - une erreur interne produit une reponse Telegram (jamais de silence).
"""
import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.bot as bot
from app.accounting_agent import AccountingAgentError

BOT_SOURCE = Path(__file__).resolve().parents[1].joinpath("app", "bot.py").read_text()


def _handler_decorators() -> list[str]:
    """Liste ordonnee des decorateurs @dp.message(...) de build_dispatcher."""
    tree = ast.parse(BOT_SOURCE)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_dispatcher":
            for inner in ast.walk(node):
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for deco in inner.decorator_list:
                        out.append(ast.unparse(deco))
    return out


def test_a_free_text_handler_is_registered():
    decorators = _handler_decorators()
    text_handlers = [d for d in decorators if "F.text" in d]
    assert text_handlers, (
        "Aucun handler de texte libre enregistre : les messages non-commande "
        "resteraient 'not handled' par aiogram."
    )


def test_free_text_handler_excludes_commands():
    text_handlers = [d for d in _handler_decorators() if "F.text" in d]
    assert any("startswith('/')" in d or 'startswith("/")' in d for d in text_handlers), (
        "Le handler texte doit exclure les commandes (~F.text.startswith('/'))."
    )


def test_free_text_handler_is_registered_after_all_command_handlers():
    decorators = _handler_decorators()
    last_command = max(i for i, d in enumerate(decorators) if "Command(" in d)
    first_text = min(i for i, d in enumerate(decorators) if "F.text" in d)
    assert first_text > last_command, (
        "Le handler texte libre doit etre enregistre APRES toutes les commandes, "
        "sinon il les capturerait silencieusement."
    )


def test_no_conversation_or_state_handler_can_swallow_messages():
    # aiogram : un FSM/StateFilter mal place capterait les messages en amont.
    assert "StateFilter" not in BOT_SOURCE
    assert "ConversationHandler" not in BOT_SOURCE


def _fake_message(text: str, chat_id: int = 999653395):
    message = MagicMock()
    message.text = text
    message.chat.id = chat_id
    message.answer = AsyncMock()
    message.bot.send_chat_action = AsyncMock()
    return message


def _free_text_handler():
    """Recupere le callback du handler texte libre depuis le dispatcher."""
    dp = bot.build_dispatcher()
    for handler in dp.message.handlers:
        if handler.callback.__name__ == "cmd_free_text":
            return handler.callback
    raise AssertionError("handler cmd_free_text introuvable")


def test_text_message_reaches_the_accounting_agent():
    handler = _free_text_handler()
    message = _fake_message("donne-moi le chiffre d'affaires total TTC")
    with patch.object(bot.accounting_agent, "answer", return_value="CA: 42,00 MAD") as answer:
        asyncio.run(handler(message))
    answer.assert_called_once()
    assert answer.call_args.args[0] == 999653395   # le chat_id du client
    message.answer.assert_awaited_once_with("CA: 42,00 MAD")


def test_typing_indicator_is_sent_while_processing():
    handler = _free_text_handler()
    message = _fake_message("chiffre d'affaires")
    with patch.object(bot.accounting_agent, "answer", return_value="ok"):
        asyncio.run(handler(message))
    message.bot.send_chat_action.assert_awaited_once()
    assert message.bot.send_chat_action.await_args.kwargs["action"] == "typing"


def test_business_error_produces_a_visible_telegram_reply():
    handler = _free_text_handler()
    message = _fake_message("chiffre d'affaires")
    with patch.object(
        bot.accounting_agent, "answer",
        side_effect=AccountingAgentError("L'onglet '04_FACTURES_VENTES' est vide."),
    ):
        asyncio.run(handler(message))
    message.answer.assert_awaited_once()
    assert "vide" in message.answer.await_args.args[0]


def test_unexpected_internal_error_still_replies_instead_of_silence():
    handler = _free_text_handler()
    message = _fake_message("chiffre d'affaires")
    with patch.object(bot.accounting_agent, "answer", side_effect=RuntimeError("boom")):
        asyncio.run(handler(message))
    message.answer.assert_awaited_once()
    reply = message.answer.await_args.args[0]
    assert "erreur interne" in reply.lower()
    assert "boom" not in reply     # aucun detail technique fuite au client


def test_timeout_produces_a_visible_reply():
    handler = _free_text_handler()
    message = _fake_message("chiffre d'affaires")
    with patch.object(bot, "AGENT_TIMEOUT_SECONDS", 0.01), \
         patch.object(bot.accounting_agent, "answer", side_effect=lambda *a: __import__("time").sleep(1)):
        asyncio.run(handler(message))
    message.answer.assert_awaited_once()
    assert "trop de temps" in message.answer.await_args.args[0].lower()


def test_empty_text_is_ignored_without_calling_the_agent():
    handler = _free_text_handler()
    message = _fake_message("   ")
    with patch.object(bot.accounting_agent, "answer") as answer:
        asyncio.run(handler(message))
    answer.assert_not_called()
    message.answer.assert_not_awaited()


def test_command_handlers_still_exist_and_are_separate():
    dp = bot.build_dispatcher()
    names = {h.callback.__name__ for h in dp.message.handlers}
    for expected in ("cmd_start", "cmd_help", "cmd_status", "cmd_connect", "cmd_free_text"):
        assert expected in names


def test_help_command_still_replies():
    dp = bot.build_dispatcher()
    handler = next(h.callback for h in dp.message.handlers if h.callback.__name__ == "cmd_help")
    message = _fake_message("/help")
    asyncio.run(handler(message))
    message.answer.assert_awaited_once()
    assert "/connect" in message.answer.await_args.args[0]
