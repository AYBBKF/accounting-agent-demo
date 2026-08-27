"""Le mode verification ne touche jamais a getUpdates.

Deux long-pollers sur un meme jeton Telegram se disputent la file
getUpdates (409). Un conteneur de verification qui partage le jeton du
bot de production doit donc pouvoir demarrer SANS long polling, tout en
gardant le worker Gmail, le heartbeat et l'ENVOI de messages.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.config import Settings


def test_polling_is_enabled_by_default():
    """La production ne doit pas changer de comportement."""
    assert Settings(TELEGRAM_BOT_TOKEN="x:y").telegram_polling_enabled is True


def test_polling_can_be_disabled_by_environment():
    reglages = Settings(TELEGRAM_BOT_TOKEN="x:y", TELEGRAM_POLLING_ENABLED="false")
    assert reglages.telegram_polling_enabled is False


def test_main_branches_on_the_flag_not_on_luck():
    """Le code de main() consulte reellement le drapeau."""
    source = Path("app/bot.py").read_text(encoding="utf-8")
    assert "settings.telegram_polling_enabled" in source
    # La branche sans polling n'appelle PAS start_polling.
    sans_polling = source.split("telegram_polling_enabled")[1]
    branche_else = sans_polling.split("else:")[1].split("await asyncio.gather")[0]
    assert "start_polling" not in branche_else
