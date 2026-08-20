"""Bot Telegram de demo (aiogram, long polling).

Un seul conteneur, aucune dependance externe (pas de Postgres/Redis/
worker/Composio/MCP). Toutes les donnees manipulees sont explicitement
fictives ("DEMO").
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject

from app.auth import is_allowed_telegram_user
from app.config import settings
from app.db import connect, init_db
from app.demo_data import generate_demo_bank_statement, generate_demo_invoices
from app.excel_report import build_excel_report
from app.openai_client import OpenAIClientWrapper
from app.reconciliation import reconcile_invoices
from app.vat import simulate_vat

FAKE_INVOICE_TEXT_FOR_EXTRACTION = (
    "FACTURE (DEMO - donnee fictive)\n"
    "Fournisseur: Fournitures Atlas SARL (DEMO)\n"
    "Numero: DEMO-2026-9001\n"
    "Date: 2026-08-15\n"
    "Montant HT: 250.00 MAD\n"
    "TVA: 20%\n"
    "Montant TTC: 300.00 MAD\n"
    "Mode de paiement: virement\n"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("demo_bot")

HEARTBEAT_PATH = Path("/app/data/heartbeat")

# Etat en memoire de la session de demo (par chat) : donnees fictives generees.
_DEMO_STATE: dict[int, dict[str, Any]] = {}


class WhitelistMiddleware:
    def __init__(self, allowed_ids: set[int]) -> None:
        self._allowed_ids = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = getattr(user, "id", None)
        if not is_allowed_telegram_user(user_id, self._allowed_ids):
            logger.warning("Utilisateur non autorise a utilise le bot: %s", user_id)
            if isinstance(event, Message):
                await event.answer("Acces refuse. Ce bot est prive.")
            return None
        return await handler(event, data)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(WhitelistMiddleware(settings.allowed_user_ids()))
    dp.callback_query.outer_middleware(WhitelistMiddleware(settings.allowed_user_ids()))

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "Bienvenue sur le bot de demo comptable.\n"
            "Toutes les donnees sont FICTIVES.\n"
            "Commandes: /help /demo_facture /demo_releve /tva /rapprochement /export /demo_extraction"
        )

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "/demo_facture - genere des factures fictives\n"
            "/demo_releve - genere un releve bancaire fictif lie aux factures\n"
            "/tva - simule la TVA sur les factures generees\n"
            "/rapprochement - rapproche factures et releve bancaire\n"
            "/export - genere et envoie le rapport Excel\n"
            "/demo_extraction - teste l'extraction OpenAI sur une facture fictive\n"
            "/status - etat du bot"
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        chat_state = _DEMO_STATE.get(message.chat.id, {})
        await message.answer(
            f"OK. Factures en session: {len(chat_state.get('invoices', []))}. "
            f"Lignes bancaires: {len(chat_state.get('bank_lines', []))}."
        )

    @dp.message(Command("demo_facture"))
    async def cmd_demo_facture(message: Message) -> None:
        invoices = generate_demo_invoices(count=5, allowed_rates=settings.vat_rates())
        _DEMO_STATE.setdefault(message.chat.id, {})["invoices"] = invoices
        lines = [f"- {i.numero} | {i.fournisseur} | HT {i.montant_ht} | TVA {i.taux_tva}%" for i in invoices]
        await message.answer("Factures fictives generees:\n" + "\n".join(lines))

    @dp.message(Command("demo_releve"))
    async def cmd_demo_releve(message: Message) -> None:
        chat_state = _DEMO_STATE.setdefault(message.chat.id, {})
        invoices = chat_state.get("invoices")
        if not invoices:
            await message.answer("Genere d'abord des factures avec /demo_facture.")
            return
        bank_lines = generate_demo_bank_statement(invoices)
        chat_state["bank_lines"] = bank_lines
        lines = [f"- {b.date_operation.isoformat()} | {b.libelle} | {b.montant}" for b in bank_lines]
        await message.answer("Releve bancaire fictif genere:\n" + "\n".join(lines))

    @dp.message(Command("tva"))
    async def cmd_tva(message: Message) -> None:
        chat_state = _DEMO_STATE.get(message.chat.id, {})
        invoices = chat_state.get("invoices")
        if not invoices:
            await message.answer("Genere d'abord des factures avec /demo_facture.")
            return
        lines = []
        for inv in invoices:
            result = simulate_vat(inv.montant_ht, inv.taux_tva, allowed_rates=settings.vat_rates())
            lines.append(
                f"- {inv.numero}: HT {result.montant_ht} + TVA({result.taux_tva}%) "
                f"{result.montant_tva} = TTC {result.montant_ttc}"
            )
        await message.answer("Simulation TVA:\n" + "\n".join(lines))

    @dp.message(Command("rapprochement"))
    async def cmd_rapprochement(message: Message) -> None:
        chat_state = _DEMO_STATE.get(message.chat.id, {})
        invoices = chat_state.get("invoices")
        bank_lines = chat_state.get("bank_lines")
        if not invoices or not bank_lines:
            await message.answer("Genere d'abord /demo_facture puis /demo_releve.")
            return
        results = reconcile_invoices(
            invoices,
            bank_lines,
            amount_tolerance=settings.reconciliation_amount_tolerance,
            window_days=settings.reconciliation_window_days,
        )
        chat_state["reconciliations"] = results
        lines = [f"- {r.invoice.numero}: {r.status} ({r.detail})" for r in results]
        await message.answer("Rapprochement bancaire:\n" + "\n".join(lines))

    @dp.message(Command("demo_extraction"))
    async def cmd_demo_extraction(message: Message) -> None:
        if not settings.openai_api_key:
            await message.answer(
                "OPENAI_API_KEY non configuree : impossible de tester l'extraction. "
                "Ceci est le comportement attendu (needs_human_review), pas une erreur."
            )
            return
        wrapper = OpenAIClientWrapper(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            store=settings.openai_store,
            timeout_seconds=settings.openai_timeout_seconds,
            max_output_tokens=settings.openai_max_output_tokens,
            reasoning_effort=settings.openai_reasoning_effort,
        )
        await message.answer(f"Extraction en cours via {settings.openai_model} (Responses API)...")
        outcome = await asyncio.to_thread(
            wrapper.extract_invoice_text, FAKE_INVOICE_TEXT_FOR_EXTRACTION
        )
        if outcome.data is None:
            await message.answer(
                f"Extraction incomplete (needs_human_review). Raison: {outcome.reason}"
            )
            return
        d = outcome.data
        await message.answer(
            "Extraction reussie (facture fictive):\n"
            f"- Fournisseur: {d.get('fournisseur')}\n"
            f"- Numero: {d.get('numero')}\n"
            f"- Date: {d.get('date_facture')}\n"
            f"- HT: {d.get('montant_ht')}\n"
            f"- Taux TVA: {d.get('taux_tva')}\n"
            f"- TTC: {d.get('montant_ttc')}\n"
            f"- needs_human_review: {outcome.needs_human_review}"
        )

    @dp.message(Command("export"))
    async def cmd_export(message: Message) -> None:
        chat_state = _DEMO_STATE.get(message.chat.id, {})
        invoices = chat_state.get("invoices")
        bank_lines = chat_state.get("bank_lines", [])
        reconciliations = chat_state.get("reconciliations", [])
        if not invoices:
            await message.answer("Genere d'abord des factures avec /demo_facture.")
            return
        output_path = f"/app/data/exports/rapport_demo_{message.chat.id}.xlsx"
        build_excel_report(invoices, bank_lines, reconciliations, output_path)
        await message.answer_document(document=open(output_path, "rb"))

    return dp


async def _heartbeat_loop() -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    while True:
        HEARTBEAT_PATH.write_text(str(time.time()))
        await asyncio.sleep(30)


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant : impossible de demarrer le bot.")
    settings.ensure_db_dir()
    init_db(settings.db_path)

    bot = Bot(token=settings.telegram_bot_token)
    dp = build_dispatcher()

    asyncio.create_task(_heartbeat_loop())
    logger.info("Demarrage du bot de demo (long polling).")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
