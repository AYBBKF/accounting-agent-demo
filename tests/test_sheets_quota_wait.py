"""Un quota de LECTURE Sheets s'attend, il n'interrompt pas un traitement.

Le quota Google Sheets ("Read requests per minute per user") est un
compteur glissant par minute qui se libere seul. En sortant
immediatement, le rapprochement bancaire s'arretait au milieu du releve :
les lignes etaient justes, mais le journal 08_RAPPROCHEMENT restait vide.

Le quota Gmail, lui, repousse sa fenetre a chaque appel : on ne l'attend
pas. Et AUCUNE ecriture n'est jamais rejouee : une ecriture donnee pour
perdue peut avoir abouti, et la rejouer creerait une double ecriture.
"""

from __future__ import annotations

import pytest

from app import mail_worker as mw


class FauxAppel:
    """Rejoue une suite de reponses a la place de l'appel reseau."""

    def __init__(self, reponses: list[object]) -> None:
        self._reponses = list(reponses)
        self.appels: list[str] = []

    def _execute_once(self, slug, arguments):
        self.appels.append(slug)
        suivant = self._reponses.pop(0)
        if isinstance(suivant, Exception):
            raise suivant
        return suivant


@pytest.fixture(autouse=True)
def _pas_d_attente_reelle(monkeypatch):
    monkeypatch.setattr("app.mail_worker.time.sleep", lambda _s: None)


def _executer(faux: FauxAppel, slug: str):
    return mw.MailWorker.execute(faux, slug, {})


def test_une_lecture_sheets_attend_puis_reussit() -> None:
    faux = FauxAppel([
        mw.RateLimited("quota"),
        mw.RateLimited("quota"),
        {"valueRanges": [{"values": [["ok"]]}]},
    ])
    data = _executer(faux, "GOOGLESHEETS_BATCH_GET")
    assert data["valueRanges"][0]["values"] == [["ok"]]
    assert faux.appels == ["GOOGLESHEETS_BATCH_GET"] * 3


def test_l_attente_de_quota_ne_consomme_pas_les_essais_de_relecture() -> None:
    """Quota, quota, hoquet passager, puis succes : tout doit passer."""
    faux = FauxAppel([
        mw.RateLimited("quota"),
        mw.RateLimited("quota"),
        mw.MailWorkerError("hoquet"),
        {"valueRanges": []},
    ])
    assert _executer(faux, "GOOGLESHEETS_BATCH_GET") == {"valueRanges": []}


def test_le_quota_gmail_n_est_jamais_attendu() -> None:
    faux = FauxAppel([mw.RateLimited("quota"), {"messages": []}])
    with pytest.raises(mw.RateLimited):
        _executer(faux, "GMAIL_FETCH_EMAILS")
    assert faux.appels == ["GMAIL_FETCH_EMAILS"]


def test_une_ecriture_n_est_jamais_rejouee_sur_quota() -> None:
    faux = FauxAppel([mw.RateLimited("quota"), {}])
    with pytest.raises(mw.RateLimited):
        _executer(faux, "GOOGLESHEETS_VALUES_UPDATE")
    assert faux.appels == ["GOOGLESHEETS_VALUES_UPDATE"]


def test_l_attente_de_quota_sheets_est_bornee() -> None:
    faux = FauxAppel([mw.RateLimited("quota")] * (mw._SHEETS_QUOTA_ATTEMPTS + 3))
    with pytest.raises(mw.RateLimited):
        _executer(faux, "GOOGLESHEETS_BATCH_GET")
    assert len(faux.appels) == mw._SHEETS_QUOTA_ATTEMPTS + 1


def test_seules_les_lectures_sheets_sont_attendues() -> None:
    assert mw.is_sheets_read("GOOGLESHEETS_BATCH_GET")
    assert mw.is_sheets_read("GOOGLESHEETS_GET_SPREADSHEET_INFO")
    assert not mw.is_sheets_read("GOOGLESHEETS_VALUES_UPDATE")
    assert not mw.is_sheets_read("GOOGLESHEETS_ADD_SHEET")
    assert not mw.is_sheets_read("GMAIL_FETCH_EMAILS")
