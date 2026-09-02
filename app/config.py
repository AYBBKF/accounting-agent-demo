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
    # Escalade de lecture. Terra relit le TEXTE, Sol relit l'IMAGE ORIGINALE.
    # Sol est le niveau le plus couteux : son nombre d'appels est plafonne
    # PAR EMAIL, sans quoi un lot de photos illisibles le declencherait
    # autant de fois qu'il contient de pieces.
    openai_model_terra: str = Field(default="gpt-5.6-terra", alias="OPENAI_MODEL_TERRA")
    openai_model_sol: str = Field(default="gpt-5.6-sol", alias="OPENAI_MODEL_SOL")
    vision_escalation_enabled: bool = Field(default=True, alias="VISION_ESCALATION_ENABLED")
    vision_max_calls_per_email: int = Field(default=6, alias="VISION_MAX_CALLS_PER_EMAIL")

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
    # L'accolade forme un OU : `{filename:pdf filename:zip filename:png
    # filename:jpg filename:jpeg}` retient aussi bien un email ne portant
    # que des PDF, qu'un email ne portant qu'une archive ZIP, qu'un email ne
    # portant qu'une facture PHOTOGRAPHIEE (PNG/JPG/JPEG), ou n'importe quelle
    # combinaison. Sans elle, un pack envoye en ZIP seul, ou une photo de
    # facture seule, n'etait jamais vu par le worker, alors meme que le code
    # sait ouvrir les archives et, desormais, ocreiser les images.
    gmail_watch_enabled: bool = Field(default=True, alias="GMAIL_WATCH_ENABLED")
    gmail_watch_chat_id: int = Field(default=0, alias="GMAIL_WATCH_CHAT_ID")
    # Long polling Telegram. A desactiver UNIQUEMENT pour un conteneur de
    # verification qui partage le jeton du bot de production : deux
    # long-pollers sur un meme jeton se disputent getUpdates (409) et
    # casseraient la production. L'ENVOI de messages, lui, ne conflicte
    # pas ; il reste actif.
    telegram_polling_enabled: bool = Field(
        default=True, alias="TELEGRAM_POLLING_ENABLED"
    )
    gmail_watch_query: str = Field(
        default=(
            "in:inbox has:attachment "
            "{filename:pdf filename:zip filename:png filename:jpg filename:jpeg}"
        ),
        alias="GMAIL_WATCH_QUERY",
    )
    gmail_watch_interval_seconds: int = Field(default=60, alias="GMAIL_WATCH_INTERVAL_SECONDS")
    gmail_watch_max_per_cycle: int = Field(default=5, alias="GMAIL_WATCH_MAX_PER_CYCLE")
    # --- multi-entreprises ------------------------------------------------
    #
    # Desactive par defaut : un deploiement qui ne lirait pas sa
    # configuration retombe sur le comportement mono-entreprise connu,
    # jamais sur un mode degrade inconnu.
    multi_tenant_enabled: bool = Field(default=False, alias="MULTI_TENANT_ENABLED")
    # Declaration d'entreprises par l'exploitant. C'est le SEUL chemin par
    # lequel une entreprise peut naitre : aucun email ne peut en creer une.
    companies_json: str = Field(default="", alias="COMPANIES_JSON")
    # Classeur comptable servant de modele fonctionnel aux nouvelles
    # entreprises. Il est COPIE, jamais modifie ni vide.
    template_sheet_id: str = Field(default="", alias="TEMPLATE_SHEET_ID")
    # Dossier Drive parent sous lequel chaque entreprise recoit le sien.
    drive_root_folder_id: str = Field(default="", alias="DRIVE_ROOT_FOLDER_ID")

    # --- creation automatique d'entreprise --------------------------------
    #
    # Desactivee par defaut : sans ce drapeau, une adresse inconnue reste
    # en quarantaine et aucune entreprise ne peut naitre d'un email.
    # Activee, un message livre a `<base>+<identifiant>@<domaine>` cree la
    # comptabilite correspondante (classeur, dossier Drive, registre) puis
    # est traite normalement.
    auto_provision_enabled: bool = Field(default=False, alias="AUTO_PROVISION_ENABLED")
    # Boite de l'exploitant. La partie locale AVANT le `+` doit
    # correspondre exactement, sinon rien n'est cree.
    auto_provision_base_address: str = Field(
        default="", alias="AUTO_PROVISION_BASE_ADDRESS"
    )
    # Plafond de securite : au-dela, plus aucune entreprise ne nait seule.
    auto_provision_max_companies: int = Field(
        default=50, alias="AUTO_PROVISION_MAX_COMPANIES"
    )
    # Reglages d'exploitation d'une entreprise creee seule. Aucune donnee
    # legale ici : l'ICE reste vide tant que personne ne l'a declare.
    auto_provision_country: str = Field(default="MA", alias="AUTO_PROVISION_COUNTRY")
    auto_provision_currency: str = Field(default="MAD", alias="AUTO_PROVISION_CURRENCY")
    auto_provision_vat_rates: str = Field(
        default="20", alias="AUTO_PROVISION_VAT_RATES"
    )

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
