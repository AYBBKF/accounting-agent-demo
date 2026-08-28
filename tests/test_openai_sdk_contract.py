"""Le SDK OpenAI epingle DOIT exposer l'API Responses.

Contexte : la version 1.59.6 initialement epinglee ne connait pas
`client.responses`. Chaque appel partait donc en `AttributeError`,
silencieusement rattrape par les gardes de `doc_vision` et de
`openai_client` : l'escalade Terra/Sol etait morte en production sans
qu'aucun test ne le voie. Ce test verrouille le contrat.
"""

from __future__ import annotations

import pathlib

import openai

REQUIREMENTS = pathlib.Path(__file__).resolve().parents[1] / "requirements.txt"


def test_le_client_openai_expose_l_api_responses() -> None:
    client = openai.OpenAI(api_key="cle-de-test-sans-appel-reseau")
    assert hasattr(client, "responses"), (
        "Le SDK openai installe n'expose pas `responses` : "
        "l'escalade Terra/Sol et l'extraction Luna seraient inertes."
    )
    assert hasattr(client.responses, "create")


def test_la_version_epinglee_est_au_moins_1_66() -> None:
    ligne = next(
        l for l in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if l.startswith("openai==")
    )
    version = ligne.split("==", 1)[1].strip()
    majeur, mineur = (int(p) for p in version.split(".")[:2])
    assert (majeur, mineur) >= (1, 66), (
        f"openai=={version} ne connait pas l'API Responses "
        "(introduite dans la 1.66)."
    )
