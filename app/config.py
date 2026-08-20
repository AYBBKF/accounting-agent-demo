"""Configuration centralisee (Pydantic Settings).

Aucune valeur par defaut sensible n'est codee en dur. Tous les secrets
proviennent exclusivement des variables d'environnement / .env, jamais
du code source de l'image.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    allowed_telegram_user_ids: str = Field(default="", alias="ALLOWED_TELEGRAM_USER_IDS")

    # --- OpenAI (Responses API uniquement) ---
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.6-luna", alias="OPENAI_MODEL")
    openai_store: bool = Field(default=False, alias="OPENAI_STORE")
    openai_timeout_seconds: float = Field(default=60.0, alias="OPENAI_TIMEOUT_SECONDS")
    openai_max_output_tokens: int = Field(default=2000, alias="OPENAI_MAX_OUTPUT_TOKENS")
    openai_reasoning_effort: str = Field(default="none", alias="OPENAI_REASONING_EFFORT")

    # --- Donnees / TVA (configurable, jamais code en dur) ---
    db_path: str = Field(default="/app/data/demo.db", alias="DB_PATH")
    default_vat_rate: Decimal = Field(default=Decimal("20.0"), alias="DEFAULT_VAT_RATE")
    vat_rates_available: str = Field(default="0,7,10,20", alias="VAT_RATES_AVAILABLE")
    reconciliation_window_days: int = Field(default=5, alias="RECONCILIATION_WINDOW_DAYS")
    reconciliation_amount_tolerance: Decimal = Field(
        default=Decimal("0.01"), alias="RECONCILIATION_AMOUNT_TOLERANCE"
    )

    def allowed_user_ids(self) -> set[int]:
        raw = self.allowed_telegram_user_ids.strip()
        if not raw:
            return set()
        return {int(x.strip()) for x in raw.split(",") if x.strip()}

    def vat_rates(self) -> list[Decimal]:
        return [Decimal(x.strip()) for x in self.vat_rates_available.split(",") if x.strip()]

    def ensure_db_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
