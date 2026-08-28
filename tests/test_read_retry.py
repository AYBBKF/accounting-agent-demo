"""Reprise des appels de LECTURE Composio.

Un lot de documents enchaine des centaines de lectures Sheets : l'API
repond alors ponctuellement en erreur (quota par minute). Sans reprise, un
document parfaitement valide echouait sur "GOOGLESHEETS_BATCH_GET a echoue".
Les ECRITURES, elles, ne doivent jamais etre rejouees : une ecriture donnee
pour perdue peut avoir abouti, et la rejouer creerait un doublon comptable.
"""
import pytest

from app.mail_worker import MailWorkerError, is_retryable_read


class FakeGateway:
    """Reprend la logique de reprise de MailWorker.execute, sans reseau."""

    def __init__(self, failures: int):
        self.calls: list[str] = []
        self._failures = failures

    def _execute_once(self, slug, arguments):
        self.calls.append(slug)
        if len(self.calls) <= self._failures:
            raise MailWorkerError(f"L'outil '{slug}' a echoue.")
        return {"ok": True}


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr("app.mail_worker.time.sleep", lambda _s: None)
    return FakeGateway


def _run(worker_cls, slug, arguments=None):
    from app.mail_worker import MailWorker

    return MailWorker.execute(worker_cls, slug, arguments or {})


def test_reads_are_declared_retryable():
    assert is_retryable_read("GOOGLESHEETS_BATCH_GET")
    assert is_retryable_read("GOOGLESHEETS_GET_SPREADSHEET_INFO")
    assert is_retryable_read("GMAIL_GET_ATTACHMENT")


def test_writes_are_never_retryable():
    for slug in ("GOOGLESHEETS_VALUES_UPDATE", "GOOGLESHEETS_CLEAR_VALUES",
                 "GOOGLEDRIVE_UPLOAD_FILE", "GOOGLESHEETS_FORMAT_CELL",
                 "GMAIL_SEND_EMAIL"):
        assert not is_retryable_read(slug), slug


def test_a_transient_read_failure_is_retried_and_succeeds(gateway):
    gw = gateway(2)
    assert _run(gw, "GOOGLESHEETS_BATCH_GET") == {"ok": True}
    assert len(gw.calls) == 3


def test_a_persistent_read_failure_still_raises(gateway):
    gw = gateway(99)
    with pytest.raises(MailWorkerError):
        _run(gw, "GOOGLESHEETS_BATCH_GET")
    assert len(gw.calls) == 3


def test_a_failed_write_is_attempted_exactly_once(gateway):
    gw = gateway(99)
    with pytest.raises(MailWorkerError):
        _run(gw, "GOOGLESHEETS_VALUES_UPDATE")
    assert len(gw.calls) == 1


def test_a_rate_limit_is_not_retried(monkeypatch):
    """Insister pendant la fenetre de quota GMAIL la repousse : on sort.

    Le quota Gmail avance sa date de deblocage a chaque appel. Celui des
    LECTURES Sheets, lui, est un compteur par minute qui se libere seul :
    son attente bornee est verifiee dans `test_sheets_quota_wait.py`.
    """
    from app.mail_worker import MailWorker, RateLimited

    monkeypatch.setattr("app.mail_worker.time.sleep", lambda _s: None)

    class Limited:
        def __init__(self):
            self.calls = []

        def _execute_once(self, slug, arguments):
            self.calls.append(slug)
            raise RateLimited("Quota atteint.")

    gw = Limited()
    with pytest.raises(RateLimited):
        MailWorker.execute(gw, "GMAIL_FETCH_EMAILS", {})
    assert len(gw.calls) == 1

    ecriture = Limited()
    with pytest.raises(RateLimited):
        MailWorker.execute(ecriture, "GOOGLESHEETS_VALUES_UPDATE", {})
    assert len(ecriture.calls) == 1


def test_rate_limit_messages_are_recognised():
    from app.mail_worker import looks_rate_limited

    assert looks_rate_limited("HTTP 429: User-rate limit exceeded.  Retry after 2026-08-28T00:32:45Z")
    assert looks_rate_limited("Quota exceeded for quota metric")
    assert not looks_rate_limited("Requested entity was not found")
