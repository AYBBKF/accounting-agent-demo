"""Garde-fous sur la configuration Composio Connect Links (`/connect`).

Verifie que le bot utilise bien un Auth Config Gmail distinct et dedie
(pas reutilise/confondu avec Sheets, Drive ou Calendar), et que sa valeur
par defaut est celle recreee suite au blocage OAuth Google ("Cette
application est bloquee") sur l'ancien Auth Config Gmail composio-manage.
"""
from app.config import Settings

# Auth Config Gmail recree apres le blocage OAuth ("app is blocked") sur
# l'ancien ac_1VhZyQnF2Xtu : meme type (Composio Managed OAuth) et meme
# scope unique gmail.readonly, mais nouvel ID cote Composio (invalide donc
# tout ancien lien /connect Gmail genere avant ce correctif).
EXPECTED_GMAIL_AUTH_CONFIG_ID = "ac_gjPyVvtCNdXS"


def test_default_gmail_auth_config_matches_recreated_managed_oauth_config():
    settings = Settings(_env_file=None)
    assert settings.composio_auth_config_gmail == EXPECTED_GMAIL_AUTH_CONFIG_ID


def test_gmail_auth_config_is_distinct_from_sheets_drive_calendar():
    settings = Settings(_env_file=None)
    ids = {
        settings.composio_auth_config_gmail,
        settings.composio_auth_config_googlesheets,
        settings.composio_auth_config_googledrive,
        settings.composio_auth_config_googlecalendar,
    }
    # 4 services => 4 Auth Configs distincts. Si deux valeurs coincident,
    # un client connectant un service autoriserait aussi un autre service
    # sans le savoir.
    assert len(ids) == 4


def test_env_override_still_wins_over_gmail_default():
    # Le defaut code en dur ne doit jamais empecher une surcharge par
    # variable d'environnement (ex. si le projet Composio change a nouveau).
    settings = Settings(_env_file=None, COMPOSIO_AUTH_CONFIG_GMAIL="ac_overridden")
    assert settings.composio_auth_config_gmail == "ac_overridden"
