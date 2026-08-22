"""Garde-fous sur la configuration Composio Connect Links (`/connect`).

Couvre deux risques distincts :

1. Le bon Auth Config Gmail est utilise, et il est distinct de ceux des
   autres services (pas de fuite de perimetre entre services).
2. Le libelle affiche pour Gmail ne promet PAS la "lecture seule" : les
   scopes OAuth par defaut de Composio (non surchargeables sans declencher
   le blocage Google) incluent https://mail.google.com/, soit un acces
   Gmail complet. La restriction reelle est appliquee cote Composio via
   l'allowlist d'outils de l'auth config, pas par le scope OAuth.
"""
from app.composio_connect import SERVICES
from app.config import Settings

# Auth Config Gmail "Composio Managed Auth" SANS surcharge de scopes.
# Historique : ac_1VhZyQnF2Xtu puis ac_gjPyVvtCNdXS (managed + scopes forces
# a gmail.readonly) ont tous deux ete bloques par Google ("Cette application
# est bloquee"), car l'app OAuth partagee de Composio n'est pas verifiee pour
# gmail.readonly. ac_0fyBzPbu5_Db (Custom OAuth) a ensuite echoue en
# redirect_uri_mismatch. Celui-ci utilise les scopes par defaut verifies.
EXPECTED_GMAIL_AUTH_CONFIG_ID = "ac_zOiFKyWk3Pac"


def test_default_gmail_auth_config_is_the_managed_unscoped_config():
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


def test_gmail_label_does_not_claim_read_only_access():
    # Le scope OAuth reellement accorde (https://mail.google.com/) est un
    # acces Gmail complet : annoncer "lecture seule" a l'utilisateur serait
    # mensonger, meme si le bot s'interdit l'ecriture cote outils.
    label = next(label for key, _, label in SERVICES if key == "gmail")
    assert "lecture seule" not in label.lower()
    assert "readonly" not in label.lower()


def test_every_service_has_a_configured_auth_config():
    settings = Settings(_env_file=None)
    by_service = {
        "gmail": settings.composio_auth_config_gmail,
        "googlesheets": settings.composio_auth_config_googlesheets,
        "googledrive": settings.composio_auth_config_googledrive,
        "googlecalendar": settings.composio_auth_config_googlecalendar,
    }
    for service_key, _, _ in SERVICES:
        assert by_service.get(service_key), f"auth config manquant pour {service_key}"


def test_the_gmail_query_no_longer_requires_the_subject_marker():
    # Le client a explicitement autorise l'elargissement : c'est desormais le
    # contenu du document qui decide de son type.
    settings = Settings(_env_file=None)
    assert settings.gmail_watch_query == "in:inbox has:attachment {filename:pdf filename:zip}"
    assert "XBLASTE" not in settings.gmail_watch_query
    assert "subject:" not in settings.gmail_watch_query


def test_the_gmail_query_covers_zip_archives_as_well_as_pdf():
    """Le code sait ouvrir les ZIP : la requete doit aussi les ramener.

    Sans le groupe OU, un email ne portant que le pack ZIP n'etait jamais
    vu par le worker - le classifieur le plus soigne n'y pouvait rien.
    """
    query = Settings(_env_file=None).gmail_watch_query
    assert "filename:pdf" in query
    assert "filename:zip" in query
    assert "{" in query and "}" in query, "sans accolade, Gmail exigerait les DEUX extensions"


def test_the_gmail_query_stays_overridable_by_environment():
    settings = Settings(_env_file=None, GMAIL_WATCH_QUERY="in:inbox from:compta@example.ma")
    assert settings.gmail_watch_query == "in:inbox from:compta@example.ma"
