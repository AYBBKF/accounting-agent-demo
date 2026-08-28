"""Bot Telegram de demo (aiogram, long polling).

Un seul conteneur, aucune dependance externe lourde (pas de Postgres/
Redis/worker). La seule integration externe est la synchronisation
Google Sheets optionnelle, via la connexion Composio deja active
(voir app/sheets_client.py) : aucun compte de service Google, aucune
cle JSON. Toutes les donnees manipulees sont explicitement fictives
("DEMO").
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
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app.accounting_agent import (
    AccountingAgent,
    AccountingAgentClarification,
    AccountingAgentError,
)
from app.agent_intent import LLMIntentRouter
from app.auth import is_allowed_telegram_user
from app.composio_connect import ComposioConnectError, ComposioConnectManager, SERVICES
from app.config import settings
from app.telegram_delivery import TelegramDeliveryError, send_text
from app.db import (
    init_db,
    save_bank_lines,
    save_invoices,
    save_reconciliations,
    stable_bank_line_id,
    stable_invoice_id,
)
from app.demo_data import generate_demo_bank_statement, generate_demo_invoices
from app.doc_policy import ACTION_AUTO, ACTION_REVIEW
from app.mail_worker import (
    MailWorker,
    MailWorkerError,
    build_review_message,
    build_summary,
)
from app.excel_report import build_excel_report
from app import doc_vision
from app.openai_client import OpenAIClientWrapper
from app.reconciliation import reconcile_invoices
from app.sheets_client import SheetsClient, SheetsSyncError
from app.vat import simulate_vat

# Saut de ligne sans sequence d'echappement : le transport JSON des outils
# de publication reinterprete les sequences d'echappement et corromprait le
# fichier. Un module sans antislash traverse la chaine intact.
NL = chr(10)

# Onglets dedies a la synchronisation du bot (distincts des onglets du
# classeur "X BLASTE" qui contiennent le jeu de donnees de demonstration
# complet). ID stable en premiere colonne pour une synchronisation idempotente.
SHEET_TAB_FACTURES = "BOT_FACTURES"
SHEET_TAB_RELEVE = "BOT_RELEVE"
SHEET_TAB_RAPPROCHEMENT = "BOT_RAPPROCHEMENT"
SHEET_TAB_TVA = "BOT_TVA"

_HEADERS_FACTURES = [
    "ID", "Chat", "Fournisseur", "Numero", "Date", "Montant HT",
    "Taux TVA", "Montant TVA", "Montant TTC", "Categorie",
]
_HEADERS_RELEVE = ["ID", "Chat", "Date operation", "Libelle", "Montant"]
_HEADERS_RAPPROCHEMENT = ["ID", "Chat", "Facture", "Statut", "Detail"]
_HEADERS_TVA = ["ID", "Chat", "Facture", "HT", "Taux TVA", "TVA", "TTC"]

sheets_client = SheetsClient(
    composio_api_key=settings.composio_api_key,
    composio_user_id=settings.composio_user_id,
    composio_connected_account_id=settings.composio_connected_account_id,
    spreadsheet_id=settings.google_sheet_id,
)

# Connexions Google multi-clients (Gmail/Sheets/Drive/Calendar), isolees par
# client Telegram via user_id = "telegram_<chat_id>" (/connect, /status).
# Independant de `sheets_client` ci-dessus, qui reste dedie au classeur de
# demo "X BLASTE" (une connexion unique, cote bot).
connect_manager = ComposioConnectManager(
    api_key=settings.composio_api_key,
    auth_config_by_service={
        "gmail": settings.composio_auth_config_gmail,
        "googlesheets": settings.composio_auth_config_googlesheets,
        "googledrive": settings.composio_auth_config_googledrive,
        "googlecalendar": settings.composio_auth_config_googlecalendar,
    },
)

# Agent comptable en langage naturel : repond aux messages texte libre en
# lisant le Google Sheet du client via SA propre connexion Composio
# (user_id = "telegram_<chat_id>"). Les montants sont calcules en Decimal
# dans app/accounting_agent.py, jamais produits par un modele de langage.
AGENT_TIMEOUT_SECONDS = 45.0

# Le routeur LLM ne sert qu'a COMPRENDRE la demande (intention, periode,
# client, facture) en francais / darija / arabe. Il ne recoit aucun montant
# et ne calcule rien. Sans cle OpenAI, un routeur de secours par mots-cles
# prend le relais : le bot reste utilisable.
_intent_router = LLMIntentRouter(
    api_key=settings.openai_api_key,
    model=settings.openai_model,
    store=settings.openai_store,
    timeout_seconds=settings.openai_timeout_seconds,
    reasoning_effort=settings.openai_reasoning_effort,
)

accounting_agent = AccountingAgent(
    api_key=settings.composio_api_key,
    spreadsheet_id=settings.google_sheet_id,
    router=_intent_router,
)

# Libelles des services affiches dans /help, derives de SERVICES pour qu'ils
# ne puissent jamais diverger des libelles reels des boutons /connect.
SERVICES_SUMMARY = ", ".join(label for _, _, label in SERVICES)

# Worker Gmail multi-documents : analyse toutes les pieces jointes des
# emails recus depuis le curseur (PDF et ZIP), identifie le TYPE de chaque
# document par son contenu, et le classe au bon endroit. Seuls les documents
# reellement ambigus passent par les boutons de validation.
# Escalade de lecture : Terra relit le texte, Sol relit l'image originale.
# Desactivable par configuration ; absente, le comportement deterministe est
# strictement inchange.
_vision_extractor = (
    doc_vision.VisionExtractor(
        api_key=settings.openai_api_key,
        model_terra=settings.openai_model_terra,
        model_sol=settings.openai_model_sol,
        timeout_seconds=settings.openai_timeout_seconds,
    )
    if settings.vision_escalation_enabled and settings.openai_api_key
    else None
)

mail_worker = MailWorker(
    api_key=settings.composio_api_key,
    chat_id=settings.gmail_watch_chat_id,
    db_path=settings.db_path,
    spreadsheet_id=settings.google_sheet_id,
    query=settings.gmail_watch_query,
    poll_seconds=settings.gmail_watch_interval_seconds,
    company_name=settings.company_name,
    drive_folder=settings.drive_archive_folder,
    max_per_cycle=settings.gmail_watch_max_per_cycle,
    zip_limits=settings.zip_limits(),
    allowed_vat_rates=tuple(settings.vat_rates()),
    vision=_vision_extractor,
    vision_max_calls=settings.vision_max_calls_per_email,
)

SYNC_SHEET_BUTTON_TEXT = "Synchroniser Google Sheets"
SYNC_SHEET_CALLBACK_DATA = "sync_sheet"

_sync_sheet_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text=SYNC_SHEET_BUTTON_TEXT, callback_data=SYNC_SHEET_CALLBACK_DATA)]
    ]
)


def _sync_invoices_to_sheet(chat_id: int, invoices: list[Any], db_ids: list[int]) -> None:
    if not sheets_client.is_configured:
        return
    try:
        rows = []
        for inv, db_id in zip(invoices, db_ids):
            vat = simulate_vat(inv.montant_ht, inv.taux_tva)
            rows.append(
                {
                    "id": stable_invoice_id(chat_id, db_id),
                    "ID": stable_invoice_id(chat_id, db_id),
                    "Chat": str(chat_id),
                    "Fournisseur": inv.fournisseur,
                    "Numero": inv.numero,
                    "Date": inv.date_facture.isoformat(),
                    "Montant HT": str(vat.montant_ht),
                    "Taux TVA": str(vat.taux_tva),
                    "Montant TVA": str(vat.montant_tva),
                    "Montant TTC": str(vat.montant_ttc),
                    "Categorie": "",
                }
            )
        sheets_client.upsert_rows(SHEET_TAB_FACTURES, _HEADERS_FACTURES, rows)
    except SheetsSyncError as exc:
        logger.warning("Sync Sheets (factures) ignoree: %s", exc)


def _sync_bank_lines_to_sheet(chat_id: int, bank_lines: list[Any], db_ids: list[int]) -> None:
    if not sheets_client.is_configured:
        return
    try:
        rows = [
            {
                "id": stable_bank_line_id(chat_id, db_id),
                "ID": stable_bank_line_id(chat_id, db_id),
                "Chat": str(chat_id),
                "Date operation": b.date_operation.isoformat(),
                "Libelle": b.libelle,
                "Montant": str(b.montant),
            }
            for b, db_id in zip(bank_lines, db_ids)
        ]
        sheets_client.upsert_rows(SHEET_TAB_RELEVE, _HEADERS_RELEVE, rows)
    except SheetsSyncError as exc:
        logger.warning("Sync Sheets (releve) ignoree: %s", exc)


def _sync_reconciliations_to_sheet(chat_id: int, results: list[Any]) -> None:
    if not sheets_client.is_configured:
        return
    try:
        rows = [
            {
                "id": f"RAPPR-{chat_id}-{r.invoice.numero}",
                "ID": f"RAPPR-{chat_id}-{r.invoice.numero}",
                "Chat": str(chat_id),
                "Facture": r.invoice.numero,
                "Statut": r.status,
                "Detail": r.detail,
            }
            for r in results
        ]
        sheets_client.upsert_rows(SHEET_TAB_RAPPROCHEMENT, _HEADERS_RAPPROCHEMENT, rows)
    except SheetsSyncError as exc:
        logger.warning("Sync Sheets (rapprochement) ignoree: %s", exc)


def _sync_vat_to_sheet(chat_id: int, invoices: list[Any]) -> None:
    if not sheets_client.is_configured:
        return
    try:
        rows = []
        for inv in invoices:
            result = simulate_vat(inv.montant_ht, inv.taux_tva, allowed_rates=settings.vat_rates())
            rows.append(
                {
                    "id": f"TVA-{chat_id}-{inv.numero}",
                    "ID": f"TVA-{chat_id}-{inv.numero}",
                    "Chat": str(chat_id),
                    "Facture": inv.numero,
                    "HT": str(result.montant_ht),
                    "Taux TVA": str(result.taux_tva),
                    "TVA": str(result.montant_tva),
                    "TTC": str(result.montant_ttc),
                }
            )
        sheets_client.upsert_rows(SHEET_TAB_TVA, _HEADERS_TVA, rows)
    except SheetsSyncError as exc:
        logger.warning("Sync Sheets (TVA) ignoree: %s", exc)

FAKE_INVOICE_TEXT_FOR_EXTRACTION = (
    f"FACTURE (DEMO - donnee fictive){NL}"
    f"Fournisseur: Fournitures Atlas SARL (DEMO){NL}"
    f"Numero: DEMO-2026-9001{NL}"
    f"Date: 2026-08-15{NL}"
    f"Montant HT: 250.00 MAD{NL}"
    f"TVA: 20%{NL}"
    f"Montant TTC: 300.00 MAD{NL}"
    f"Mode de paiement: virement{NL}"
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


async def _reply_chunked(message: Any, text: str) -> None:
    """Repond a une commande en decoupant si le texte est trop long.

    `message.answer` ne prend pas de `chat_id` : on l'adapte a la signature
    attendue par `send_text`, plutot que de dupliquer la logique de
    decoupage et de reessai a deux endroits.
    """
    async def envoyer(*, chat_id: int, text: str) -> Any:  # noqa: ARG001
        return await message.answer(text)

    await send_text(envoyer, 0, text, label="reponse")


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(WhitelistMiddleware(settings.allowed_user_ids()))
    dp.callback_query.outer_middleware(WhitelistMiddleware(settings.allowed_user_ids()))

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        await message.answer(
            f"Bienvenue sur le bot de demo comptable.{NL}"
            f"Toutes les donnees sont FICTIVES.{NL}"
            "Commandes: /help /demo_facture /demo_releve /tva /rapprochement /export "
            "/demo_extraction /sheet /sync_sheet /dashboard /reprocess /retry_pending"
            " /resend_pending /connect /status"
        )

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            f"/demo_facture - genere des factures fictives{NL}"
            f"/demo_releve - genere un releve bancaire fictif lie aux factures{NL}"
            f"/tva - simule la TVA sur les factures generees{NL}"
            f"/rapprochement - rapproche factures et releve bancaire{NL}"
            f"/export - genere et envoie le rapport Excel{NL}"
            f"/demo_extraction - teste l'extraction OpenAI sur une facture fictive{NL}"
            f"/sheet - lien vers le Google Sheet de suivi (si configure){NL}"
            f"/sync_sheet - resynchronise toutes les donnees de session vers le Sheet{NL}"
            f"/dashboard - resume des KPI de la session en cours{NL}"
            f"/reprocess - relit les emails des N dernieres heures (defaut 24){NL}"
            f"/retry_pending - relance les documents restes en attente{NL}"
            f"/resend_pending - renvoie les validations encore en attente{NL}"
            f"/connect - connecte TES comptes Google ({SERVICES_SUMMARY}) "
            f"via un lien d'autorisation individuel{NL}"
            "/status - etat du bot + statut de tes connexions Google"
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        chat_state = _DEMO_STATE.get(message.chat.id, {})
        lines = [
            f"OK. Factures en session: {len(chat_state.get('invoices', []))}. "
            f"Lignes bancaires: {len(chat_state.get('bank_lines', []))}.",
            "",
            "Connexions Google (ce client uniquement):",
        ]
        if not connect_manager.is_configured:
            lines.append("- non configurees sur ce bot pour l'instant.")
        else:
            try:
                statuses = await asyncio.to_thread(connect_manager.get_status, message.chat.id)
                for service_key, _, label in SERVICES:
                    entry = statuses.get(service_key, {})
                    icon = "OK" if entry.get("status") == "ACTIVE" else "--"
                    lines.append(f"- [{icon}] {label}: {entry.get('status_label', 'non connecte')}")
                lines.append("")
                lines.append("Pour connecter ou reconnecter un service: /connect")
            except ComposioConnectError as exc:
                lines.append(f"- lecture du statut impossible: {exc}")
        await message.answer(f"{NL}".join(lines))

    @dp.message(Command("connect"))
    async def cmd_connect(message: Message) -> None:
        if not connect_manager.is_configured:
            await message.answer(
                "Connexions Google non configurees sur ce bot pour l'instant "
                "(COMPOSIO_API_KEY / COMPOSIO_AUTH_CONFIG_* manquants)."
            )
            return
        await message.answer("Generation de tes liens de connexion Google...")
        try:
            results = await asyncio.to_thread(connect_manager.create_links, message.chat.id)
        except ComposioConnectError as exc:
            await message.answer(f"Impossible de generer les liens de connexion: {exc}")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"Connecter {r.label}", url=r.redirect_url)]
                for r in results
            ]
        )
        await message.answer(
            "Clique sur le service a connecter (tu seras redirige vers la page "
            "d'autorisation Google - aucun mot de passe ne transite par ce bot). "
            f"Chaque lien est valable quelques minutes ; relance /connect si besoin.{NL}"
            "Une fois autorise, verifie avec /status.",
            reply_markup=keyboard,
        )

    @dp.message(Command("demo_facture"))
    async def cmd_demo_facture(message: Message) -> None:
        invoices = generate_demo_invoices(count=5, allowed_rates=settings.vat_rates())
        _DEMO_STATE.setdefault(message.chat.id, {})["invoices"] = invoices
        db_ids = save_invoices(settings.db_path, message.chat.id, invoices)
        _sync_invoices_to_sheet(message.chat.id, invoices, db_ids)
        _sync_vat_to_sheet(message.chat.id, invoices)
        lines = [f"- {i.numero} | {i.fournisseur} | HT {i.montant_ht} | TVA {i.taux_tva}%" for i in invoices]
        await message.answer(f"Factures fictives generees:{NL}" + f"{NL}".join(lines))

    @dp.message(Command("demo_releve"))
    async def cmd_demo_releve(message: Message) -> None:
        chat_state = _DEMO_STATE.setdefault(message.chat.id, {})
        invoices = chat_state.get("invoices")
        if not invoices:
            await message.answer("Genere d'abord des factures avec /demo_facture.")
            return
        bank_lines = generate_demo_bank_statement(invoices)
        chat_state["bank_lines"] = bank_lines
        db_ids = save_bank_lines(settings.db_path, message.chat.id, bank_lines)
        _sync_bank_lines_to_sheet(message.chat.id, bank_lines, db_ids)
        lines = [f"- {b.date_operation.isoformat()} | {b.libelle} | {b.montant}" for b in bank_lines]
        await message.answer(f"Releve bancaire fictif genere:{NL}" + f"{NL}".join(lines))

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
        _sync_vat_to_sheet(message.chat.id, invoices)
        await message.answer(f"Simulation TVA:{NL}" + f"{NL}".join(lines))

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
        save_reconciliations(settings.db_path, message.chat.id, results)
        _sync_reconciliations_to_sheet(message.chat.id, results)
        lines = [f"- {r.invoice.numero}: {r.status} ({r.detail})" for r in results]
        await message.answer(f"Rapprochement bancaire:{NL}" + f"{NL}".join(lines))

    @dp.message(Command("sheet"))
    async def cmd_sheet(message: Message) -> None:
        url = sheets_client.sheet_url()
        if not url:
            await message.answer(
                "Aucun Google Sheet configure pour l'instant "
                "(GOOGLE_SHEET_ID manquant)."
            )
            return
        await message.answer(f"Google Sheet de suivi:{NL}{url}")

    async def _run_sync_sheet(chat_id: int) -> str:
        """Synchronisation idempotente SQLite -> Google Sheets, partagee
        entre la commande /sync_sheet et le bouton inline. Aucun appel
        OpenAI n'a lieu ici."""
        if not sheets_client.is_configured:
            return (
                "Synchronisation Google Sheets non configuree sur ce bot "
                "(COMPOSIO_API_KEY / COMPOSIO_USER_ID ou "
                "COMPOSIO_CONNECTED_ACCOUNT_ID / GOOGLE_SHEET_ID manquants)."
            )
        chat_state = _DEMO_STATE.get(chat_id, {})
        invoices = chat_state.get("invoices", [])
        bank_lines = chat_state.get("bank_lines", [])
        reconciliations = chat_state.get("reconciliations", [])
        if not invoices and not bank_lines:
            return "Rien a synchroniser: genere d'abord des donnees de demo."
        inv_db_ids = save_invoices(settings.db_path, chat_id, invoices) if invoices else []
        bank_db_ids = save_bank_lines(settings.db_path, chat_id, bank_lines) if bank_lines else []
        if invoices:
            _sync_invoices_to_sheet(chat_id, invoices, inv_db_ids)
            _sync_vat_to_sheet(chat_id, invoices)
        if bank_lines:
            _sync_bank_lines_to_sheet(chat_id, bank_lines, bank_db_ids)
        if reconciliations:
            save_reconciliations(settings.db_path, chat_id, reconciliations)
            _sync_reconciliations_to_sheet(chat_id, reconciliations)
        return (
            "Synchronisation terminee (idempotente, sans doublon). "
            f"Factures: {len(invoices)}, lignes bancaires: {len(bank_lines)}, "
            f"rapprochements: {len(reconciliations)}."
        )

    @dp.message(Command("sync_sheet"))
    async def cmd_sync_sheet(message: Message) -> None:
        result = await _run_sync_sheet(message.chat.id)
        await message.answer(result, reply_markup=_sync_sheet_keyboard)

    @dp.callback_query(F.data == SYNC_SHEET_CALLBACK_DATA)
    async def cb_sync_sheet(callback: CallbackQuery) -> None:
        await callback.answer("Synchronisation en cours...")
        result = await _run_sync_sheet(callback.message.chat.id)
        await callback.message.answer(result, reply_markup=_sync_sheet_keyboard)

    @dp.message(Command("dashboard"))
    async def cmd_dashboard(message: Message) -> None:
        chat_state = _DEMO_STATE.get(message.chat.id, {})
        invoices = chat_state.get("invoices", [])
        bank_lines = chat_state.get("bank_lines", [])
        reconciliations = chat_state.get("reconciliations", [])
        total_ht = sum((i.montant_ht for i in invoices), Decimal("0"))
        total_ttc = sum(
            (simulate_vat(i.montant_ht, i.taux_tva).montant_ttc for i in invoices), Decimal("0")
        )
        matched = sum(1 for r in reconciliations if r.status == "rapprochee")
        lines = [
            "Tableau de bord (session en cours, donnees FICTIVES):",
            f"- Factures: {len(invoices)} | Total HT: {total_ht} | Total TTC: {total_ttc}",
            f"- Lignes bancaires: {len(bank_lines)}",
            f"- Rapprochements: {len(reconciliations)} ({matched} rapproches)",
        ]
        url = sheets_client.sheet_url()
        if url:
            lines.append(f"- Google Sheet: {url}")
        await message.answer(f"{NL}".join(lines), reply_markup=_sync_sheet_keyboard)

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
            f"Extraction reussie (facture fictive):{NL}"
            f"- Fournisseur: {d.get('fournisseur')}{NL}"
            f"- Numero: {d.get('numero')}{NL}"
            f"- Date: {d.get('date_facture')}{NL}"
            f"- HT: {d.get('montant_ht')}{NL}"
            f"- Taux TVA: {d.get('taux_tva')}{NL}"
            f"- TTC: {d.get('montant_ttc')}{NL}"
            f"- needs_human_review: {outcome.needs_human_review}"
        )

    @dp.message(Command("reprocess"))
    async def cmd_reprocess(message: Message) -> None:
        """Reprise volontaire : recule le curseur Gmail de N heures.

        Les documents deja traites restent proteges par leur cle
        d'idempotence : reculer le curseur fait RELIRE des emails, jamais
        reecrire des lignes.
        """
        parts = (message.text or "").split()
        try:
            hours = max(1, min(720, int(parts[1]))) if len(parts) > 1 else 24
        except ValueError:
            await message.answer("Usage : /reprocess [heures] (defaut 24, maximum 720).")
            return
        floor = await asyncio.to_thread(mail_worker.rewind, hours)
        await message.answer(
            f"Curseur Gmail recule de {hours} h. Les emails recus depuis cette "
            "date seront relus au prochain cycle ; les documents deja "
            f"enregistres ne seront pas reecrits.{NL}"
            f"Nouvelle borne : {floor}"
        )

    @dp.message(Command("retry_pending"))
    async def cmd_retry_pending(message: Message) -> None:
        """Relance les documents restes en plan, sans jamais rien dupliquer.

        Reservee au proprietaire du suivi Gmail : un autre utilisateur
        autorise ne doit pas pouvoir declencher la reprise des documents
        d'autrui. Les lignes deja ecrites sont protegees par l'etat de chaque
        document ; la reprise ne fait que terminer ce qui manque et
        replacer dans 21_A_VERIFIER les documents qui n'y figurent pas
        encore. Aucune validation n'est proposee : ce bot n'en a plus.
        """
        if message.chat.id != settings.gmail_watch_chat_id:
            await message.answer("Commande reservee au proprietaire du suivi Gmail.")
            return
        await message.answer("Reprise des documents en attente...")
        try:
            outcomes = await asyncio.to_thread(mail_worker.retry_pending)
        except MailWorkerError as exc:
            await message.answer(f"Reprise impossible : {exc}")
            return
        if not outcomes:
            await message.answer("Aucun document en attente. Rien a reprendre.")
            return
        finished = [o for o in outcomes if o.action == ACTION_AUTO and not o.error]
        waiting = [o for o in outcomes if o.action == ACTION_REVIEW and not o.error]
        failed = [o for o in outcomes if o.error]
        await message.answer(
            f"Reprise terminee.{NL}"
            f"- Termines sans intervention : {len(finished)}{NL}"
            f"- Ecarts dans 21_A_VERIFIER  : {len(waiting)}{NL}"
            f"- En echec                   : {len(failed)}"
        )
        for outcome in waiting:
            await _reply_chunked(message, build_review_message(outcome))
            await asyncio.to_thread(mail_worker.mark_notified, outcome)
        for outcome in failed:
            await message.answer(f"{outcome.filename} : {outcome.error}")

    @dp.message(Command("a_verifier"))
    async def cmd_a_verifier(message: Message) -> None:
        """Rappelle les documents ecartes de la comptabilite.

        Purement informatif : ces documents sont deja inscrits en rouge
        dans 21_A_VERIFIER, avec leur motif. Cette commande ne fait que
        les relister dans Telegram. Aucune ecriture Sheets, Drive ou
        Calendar n'est declenchee ici, et aucune validation n'est
        proposee - la correction se fait dans le classeur, ou en le
        demandant explicitement au bot.
        """
        if message.chat.id != settings.gmail_watch_chat_id:
            await message.answer("Commande reservee au proprietaire du suivi Gmail.")
            return
        try:
            outcomes = await asyncio.to_thread(mail_worker.pending_validations)
        except MailWorkerError as exc:
            await message.answer(f"Lecture impossible : {exc}")
            return
        if not outcomes:
            await message.answer("Aucun document en attente dans 21_A_VERIFIER.")
            return
        await message.answer(
            f"Documents ecartes de la comptabilite : {len(outcomes)}. "
            f"Ils sont ecrits en rouge dans 21_A_VERIFIER."
        )
        for outcome in outcomes:
            await _reply_chunked(message, build_review_message(outcome))
            await asyncio.to_thread(mail_worker.mark_notified, outcome)

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

    # --- Handler texte libre : DOIT rester le dernier enregistre ---
    # aiogram evalue les handlers dans l'ordre d'enregistrement : place ici,
    # ce filtre attrape tout message texte qui n'est pas une commande, sans
    # jamais court-circuiter les commandes ci-dessus. Avant ce handler,
    # aiogram journalisait "Update ... is not handled" et le bot restait muet.
    @dp.message(F.text & ~F.text.startswith("/"))
    async def cmd_free_text(message: Message) -> None:
        question = (message.text or "").strip()
        if not question:
            return
        chat_id = message.chat.id
        logger.info(
            "Question libre recue (chat=%s, longueur=%s)", chat_id, len(question)
        )
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:  # noqa: BLE001 - l'indicateur ne doit jamais bloquer
            logger.debug("Indicateur 'typing' indisponible (chat=%s)", chat_id)
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(accounting_agent.answer, chat_id, question),
                timeout=AGENT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Agent comptable: delai depasse (chat=%s, timeout=%ss)",
                chat_id,
                AGENT_TIMEOUT_SECONDS,
            )
            await message.answer(
                "Le traitement a pris trop de temps (plus de "
                f"{int(AGENT_TIMEOUT_SECONDS)}s). Reessaie dans un moment."
            )
            return
        except AccountingAgentClarification as exc:
            logger.info("Agent comptable: precision demandee (chat=%s)", chat_id)
            await message.answer(str(exc))
            return
        except AccountingAgentError as exc:
            # Erreur metier : message deja formule pour le client, sans secret.
            logger.warning("Agent comptable: erreur metier (chat=%s): %s", chat_id, exc)
            await message.answer(str(exc))
            return
        except Exception:  # noqa: BLE001 - jamais de silence cote client
            logger.exception("Agent comptable: erreur inattendue (chat=%s)", chat_id)
            await message.answer(
                "Une erreur interne est survenue pendant l'analyse de ton classeur. "
                "L'incident est journalise ; reessaie dans un moment ou utilise /help."
            )
            return
        await message.answer(reply)

    return dp


async def deliver_summary(bot: Any, summary: Any) -> dict[str, Any]:
    """Annonce UN email, et ne marque que ce qui a REELLEMENT ete delivre.

    Le defaut corrige ici etait un mensonge d'etat. Quand l'envoi du
    resume echouait, les documents importes ou classes etaient malgre tout
    passes a `mark_notified` : le cycle suivant les croyait annonces et ne
    les renvoyait jamais. Sheets et Drive avaient bouge, le client n'avait
    rien recu, et plus rien ne le rattraperait.

    `summary_delivered` est la preuve exigee. Il ne passe a True que
    lorsque TOUS les morceaux du resume ont ete confirmes par Telegram.
    Les documents ecartes de la comptabilite, eux, ont chacun leur propre
    message et leur propre preuve : ils ne dependent pas du resume.

    Rend un compte-rendu exploitable par un test comme par un journal.
    """
    rapport: dict[str, Any] = {
        "message_id": summary.message_id,
        "summary_delivered": False,
        "marked": [],
        "failed": [],
    }
    if not summary.should_notify:
        rapport["skipped"] = True
        return rapport

    chat_id = settings.gmail_watch_chat_id
    try:
        morceaux = await send_text(
            bot.send_message, chat_id, build_summary(summary), label="resume",
        )
        rapport["summary_delivered"] = bool(morceaux)
    except TelegramDeliveryError as exc:
        logger.error(
            "Resume de l'email %s NON delivre (%s) : les documents importes "
            "ou classes ne sont PAS marques notifies, ils seront reannonces "
            "au prochain cycle.", summary.message_id, exc,
        )
        rapport["failed"].append(("resume", str(exc)))

    # Chaque document ecarte porte sa propre preuve d'envoi. L'echec de
    # l'un ne prive pas le client des autres, et ne marque que lui.
    for outcome in summary.to_review:
        try:
            envoyes = await send_text(
                bot.send_message, chat_id,
                build_review_message(outcome), label="document",
            )
        except TelegramDeliveryError as exc:
            logger.error(
                "Document %s non annonce (%s) : sera reessaye au prochain "
                "cycle.", outcome.doc_key[:12], exc,
            )
            rapport["failed"].append((outcome.doc_key, str(exc)))
            continue
        await asyncio.to_thread(
            mail_worker.mark_notified, outcome,
            telegram_message_id=envoyes[0] if envoyes else 0,
        )
        rapport["marked"].append(outcome.doc_key)

    # Les documents importes ou classes n'ont PAS de message a eux : c'est
    # le resume qui les annonce. Sans resume delivre, ils n'ont ete
    # annonces nulle part, et les marquer serait faux.
    if rapport["summary_delivered"]:
        for outcome in summary.notified_outcomes:
            if outcome in summary.to_review:
                continue
            await asyncio.to_thread(mail_worker.mark_notified, outcome)
            rapport["marked"].append(outcome.doc_key)

    logger.info(
        "Email %s traite : %d document(s), resume delivre=%s, %d marque(s) "
        "notifie(s), %d en echec, %d sans changement",
        summary.message_id, len(summary.outcomes),
        rapport["summary_delivered"], len(rapport["marked"]),
        len(rapport["failed"]), summary.silenced,
    )
    return rapport


async def _gmail_watch_loop(bot: Bot) -> None:
    """Boucle de fond : interroge Gmail toutes les N secondes, importe les
    factures certaines et notifie le client. Les boutons de validation ne
    sont joints que si un doute subsiste. Ne s'arrete jamais sur une erreur
    ponctuelle."""
    if not settings.gmail_watch_enabled or not mail_worker.is_configured:
        logger.info(
            "Worker Gmail desactive (GMAIL_WATCH_ENABLED=%s, chat_id=%s).",
            settings.gmail_watch_enabled, settings.gmail_watch_chat_id,
        )
        return
    logger.info(
        "Worker Gmail demarre (user=%s, intervalle=%ss, requete=%r).",
        mail_worker.user_id, mail_worker.poll_seconds, mail_worker.query,
    )
    while True:
        try:
            summaries = await asyncio.to_thread(mail_worker.process_once)
            for summary in summaries:
                await deliver_summary(bot, summary)
        except MailWorkerError as exc:
            logger.warning("Cycle Gmail en echec: %s", exc)
        except Exception:  # noqa: BLE001 - la boucle ne doit jamais mourir
            logger.exception("Erreur inattendue dans le worker Gmail")
        await asyncio.sleep(mail_worker.poll_seconds)


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

    heartbeat = asyncio.create_task(_heartbeat_loop())
    gmail = asyncio.create_task(_gmail_watch_loop(bot))
    if settings.telegram_polling_enabled:
        logger.info("Demarrage du bot de demo (long polling).")
        await dp.start_polling(bot)
    else:
        # Mode verification : le worker Gmail et le heartbeat tournent, les
        # ENVOIS Telegram restent possibles, mais aucun getUpdates n'est
        # emis - deux long-pollers sur un meme jeton se disputeraient la
        # file (409) et casseraient l'instance de production.
        logger.info(
            "Demarrage du bot de demo SANS long polling Telegram "
            "(TELEGRAM_POLLING_ENABLED=false) : worker Gmail seul."
        )
        await asyncio.gather(heartbeat, gmail)


if __name__ == "__main__":
    asyncio.run(main())
