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
    # Limites de depaquetage ZIP. Seul le NOMBRE de fichiers est
    # remonte ; le volume total reste a 60 Mo, ce qui borne la bombe
    # de decompression independamment du nombre de membres.
    zip_max_files: int = Field(default=120, alias="ZIP_MAX_FILES")
    zip_max_total_mb: int = Field(default=60, alias="ZIP_MAX_TOTAL_MB")
    reconciliation_window_days: int = Field(default=5, alias="RECONCILIATION_WINDOW_DAYS")
    reconciliation_amount_tolerance: Decimal = Field(
        default=Decimal("0.01"), alias="RECONCILIATION_AMOUNT_TOLERANCE"
    )

    # --- Google Sheets (synchronisation, optionnelle, via Composio) ---
    # Utilise la connexion Google Sheets deja active dans Composio (OAuth) :
    # aucun compte de service Google, aucune cle JSON. Aucune valeur par
    # defaut sensible : si absent, la synchronisation est simplement
    # desactivee (le bot ne plante jamais).
    composio_api_key: str = Field(default="", alias="COMPOSIO_API_KEY")
    composio_user_id: str = Field(default="", alias="COMPOSIO_USER_ID")
    composio_connected_account_id: str = Field(
        default="", alias="COMPOSIO_CONNECTED_ACCOUNT_ID"
    )
    google_sheet_id: str = Field(default="", alias="GOOGLE_SHEET_ID")

    # --- Composio Connect Links multi-client (Gmail, Sheets, Drive, Calendar) ---
    # Un auth config par toolkit, cree une seule fois pour tout le projet
    # Composio du bot (composio-managed OAuth : AUCUN client_id/secret Google
    # a fournir, pour les 4 services). Chaque client genere sa propre
    # connexion via /connect, isolee par user_id = "telegram_<chat_id>".
    # Valeurs par defaut = auth configs du projet pr_76EmxezsdHvO ;
    # surchargeables par env si le projet change.
    #
    # Gmail: NE JAMAIS surcharger `credentials.scopes` sur l'auth config
    # managee. L'application OAuth partagee de Composio est verifiee par
    # Google pour un jeu de scopes precis (userinfo/contacts +
    # https://mail.google.com/) qui n'inclut PAS gmail.readonly. Demander
    # gmail.readonly dessus fait echouer Google en "Cette application est
    # bloquee" (verifie sur 2 auth configs managees successives). On utilise
    # donc les scopes par defaut de Composio, non surcharges.
    #
    # Contrepartie: ces scopes par defaut donnent un acces Gmail COMPLET au
    # niveau OAuth (https://mail.google.com/), plus large que la lecture
    # seule. La restriction "lecture seule" est donc appliquee cote Composio
    # via `restrict_to_following_tools` sur l'auth config (allowlist de 10
    # outils de lecture: FETCH_EMAILS, GET_ATTACHMENT, LIST_MESSAGES...),
    # ce qui rend tout envoi/suppression impossible depuis ce bot. Le libelle
    # du bouton /connect ne dit donc PAS "lecture seule" (cf. SERVICES dans
    # app/composio_connect.py).
    composio_auth_config_gmail: str = Field(default="ac_zOiFKyWk3Pac", alias="COMPOSIO_AUTH_CONFIG_GMAIL")
    composio_auth_config_googlesheets: str = Field(
        default="ac_1zYvYaXY82zA", alias="COMPOSIO_AUTH_CONFIG_GOOGLESHEETS"
    )
    composio_auth_config_googledrive: str = Field(
        default="ac_CLzIDDkOVwLz", alias="COMPOSIO_AUTH_CONFIG_GOOGLEDRIVE"
    )
    composio_auth_config_googlecalendar: str = Field(
        default="ac_u-hiMj7UNB3-", alias="COMPOSIO_AUTH_CONFIG_GOOGLECALENDAR"
    )

    # --- Worker Gmail (detection automatique des factures) ---------------
    # Desactive tant que GMAIL_WATCH_CHAT_ID vaut 0 : le bot demarre alors
    # exactement comme avant.
    #
    # La requete ne filtre plus sur un marqueur de sujet : c'est le CONTENU
    # du document qui decide de son type (voir app/doc_types.py). Deux
    # garde-fous rendent cet elargissement fiable : un curseur Gmail durable,
    # fixe au premier demarrage, qui empeche d'importer l'historique de la
    # boite ; et l'anti-doublon par empreinte du fichier puis par
    # (identifiant du tiers + numero du document).
    #
    # L'accolade forme un OU : `{filename:pdf filename:zip}` retient aussi
    # bien un email ne portant que des PDF, qu'un email ne portant qu'une
    # archive ZIP, que les deux a la fois. Sans elle, un pack envoye en ZIP
    # seul n'etait jamais vu par le worker, alors meme que le code sait
    # depuis toujours ouvrir les archives.
    gmail_watch_enabled: bool = Field(default=True, alias="GMAIL_WATCH_ENABLED")
    gmail_watch_chat_id: int = Field(default=0, alias="GMAIL_WATCH_CHAT_ID")
    gmail_watch_query: str = Field(
        default="in:inbox has:attachment {filename:pdf filename:zip}",
        alias="GMAIL_WATCH_QUERY",
    )
    gmail_watch_interval_seconds: int = Field(default=60, alias="GMAIL_WATCH_INTERVAL_SECONDS")
    gmail_watch_max_per_cycle: int = Field(default=5, alias="GMAIL_WATCH_MAX_PER_CYCLE")
    company_name: str = Field(default="X BLASTE", alias="COMPANY_NAME")
    drive_archive_folder: str = Field(
        default="XBLASTE - Factures", alias="DRIVE_ARCHIVE_FOLDER"
    )

    def allowed_user_ids(self) -> set[int]:
        raw = self.allowed_telegram_user_ids.strip()
        if not raw:
            return set()
        return {int(x.strip()) for x in raw.split(",") if x.strip()}

    def zip_limits(self) -> "ZipLimits":
        """Limites de depaquetage effectives, protections comprises.

        Profondeur, ratio de compression et taille unitaire ne sont PAS
        configurables : ce sont des protections, pas des reglages.
        """
        from app.attachments import ZipLimits

        return ZipLimits(
            max_files=int(self.zip_max_files),
            max_total_bytes=int(self.zip_max_total_mb) * 1024 * 1024,
        )

    def vat_rates(self) -> list[Decimal]:
        return [Decimal(x.strip()) for x in self.vat_rates_available.split(",") if x.strip()]

    def ensure_db_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
