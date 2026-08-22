"""Tests du worker Gmail : archives ZIP, emails multi-pieces, curseur.

Gmail est simule (aucun appel reseau, aucun envoi, aucune suppression) ;
le classeur, Drive et Calendar sont le faux classeur en memoire. Ce qui est
prouve ici, ce sont les trois points que le pipeline seul ne couvre pas :

  14. une archive ZIP est ouverte de bout en bout, en toute securite ;
  15. plusieurs pieces jointes dans un meme email sont traitees
      independamment - une piece en echec n'empeche pas les autres ;
  18. le curseur Gmail avance reellement et ne revient jamais en arriere.
"""
import io
import tempfile
import zipfile
from pathlib import Path

import pytest

from app import doc_store as store
from app.attachments import ZipLimits, idempotency_key
from app.db import init_db
from app.doc_extract import extract_document
from app.doc_policy import ACTION_AUTO, ACTION_DUPLICATE
from app.mail_worker import MailWorker, MailWorkerError, build_summary
from workbook_fake import FakeWorkbook

PACK = Path(__file__).parent / "fixtures" / "pack"

ACHAT = "01_FACTURE_ACHAT_OK_FAC-TEST-2026-002"
VENTE = "02_FACTURE_VENTE_OK_FAC-VTE-TEST-2026-012"
DEVIS = "03_DEVIS_DEV-2026-008"
RECU = "11_RECU_PAIEMENT_REC-2026-017"

CHAT_ID = 999653395
USER_ID = f"telegram_{CHAT_ID}"

GMAIL_READ_ONLY = {
    "GMAIL_FETCH_EMAILS",
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
    "GMAIL_GET_ATTACHMENT",
}


def text_of(name: str) -> str:
    return (PACK / f"{name}.txt").read_text(encoding="utf-8")


def pdf_bytes(name: str, tag: str = "") -> bytes:
    return f"%PDF-{name}-{tag}".encode()


def zip_of(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class FakeMailWorker(MailWorker):
    """Worker reel, transport Gmail simule.

    Seuls `execute` et `download` sont remplaces : la logique de cycle, de
    curseur, d'idempotence et de resume est celle de production.
    """

    def __init__(self, workbook: FakeWorkbook, db_path: str, **kwargs):
        super().__init__(
            api_key="fake", chat_id=CHAT_ID, db_path=db_path,
            spreadsheet_id="sheet-test", **kwargs,
        )
        self.workbook = workbook
        self.messages: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.gmail_calls: list[tuple[str, dict]] = []
        self.download_failures: set[str] = set()

    # -- alimentation du faux Gmail ---------------------------------------

    def moment(self, offset: int = 0) -> int:
        """Instant posterieur au curseur, donc reellement vu par le cycle."""
        return store.query_floor(self.cursor()) + 3_600 + offset

    def add_message(self, message_id: str, *, internal_date: int, attachments: dict[str, bytes],
                    subject: str = "Documents") -> None:
        listing = []
        for index, (filename, payload) in enumerate(attachments.items(), start=1):
            attachment_id = f"{message_id}-att-{index}"
            self.blobs[attachment_id] = payload
            listing.append({"attachmentId": attachment_id, "filename": filename})
        self.messages[message_id] = {
            "messageId": message_id, "subject": subject,
            "sender": "compta@example.ma", "internalDate": str(internal_date * 1000),
            "attachmentList": listing,
        }

    # -- transport simule --------------------------------------------------

    def execute(self, slug: str, arguments: dict) -> dict:
        assert arguments is not None
        if slug.startswith("GMAIL_"):
            self.gmail_calls.append((slug, arguments))
            return self._gmail(slug, arguments)
        return self.workbook.execute(slug, arguments)

    def _gmail(self, slug: str, arguments: dict) -> dict:
        if slug == "GMAIL_FETCH_EMAILS":
            floor = int(str(arguments["query"]).rsplit("after:", 1)[1])
            found = [
                {"messageId": mid}
                for mid, msg in sorted(self.messages.items())
                if int(msg["internalDate"]) // 1000 >= floor
            ]
            return {"messages": found[: arguments.get("max_results", 5)]}
        if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            message = self.messages.get(arguments["message_id"])
            if message is None:
                raise MailWorkerError("Email introuvable.")
            return dict(message)
        if slug == "GMAIL_GET_ATTACHMENT":
            return {"file": {"s3url": f"https://example.invalid/{arguments['attachment_id']}"}}
        raise AssertionError(f"outil Gmail non simule : {slug}")

    def download(self, message_id: str, attachment: dict) -> tuple[bytes, str]:
        attachment_id = str(attachment.get("attachmentId"))
        if attachment_id in self.download_failures:
            raise MailWorkerError("Telechargement de la piece jointe impossible.")
        return self.blobs[attachment_id], f"https://example.invalid/{attachment_id}"


@pytest.fixture
def db_path():
    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def worker(db_path, monkeypatch):
    import app.doc_pipeline as module

    registry: dict[bytes, str] = {}
    for name in (ACHAT, VENTE, DEVIS, RECU):
        registry[pdf_bytes(name)] = text_of(name)
        registry[pdf_bytes(name, "bis")] = text_of(name)

    def fake_read(content, company="X BLASTE", ocr=True):
        if content not in registry:
            raise ValueError("PDF illisible")
        return extract_document([registry[content]], company=company)

    monkeypatch.setattr(module, "extract_from_pdf_bytes", fake_read)
    return FakeMailWorker(FakeWorkbook(), db_path)


# === 14. archive ZIP ouverte de bout en bout ==============================

def test_a_zip_attachment_is_opened_and_every_pdf_inside_is_processed(worker):
    worker.add_message(
        "m-zip", internal_date=worker.moment(0),
        attachments={"lot.zip": zip_of({
            "achat.pdf": pdf_bytes(ACHAT),
            "dossier/vente.pdf": pdf_bytes(VENTE),
            "dossier/devis.pdf": pdf_bytes(DEVIS),
        })},
    )
    summaries = worker.process_once()

    assert len(summaries) == 1
    outcomes = summaries[0].outcomes
    assert len(outcomes) == 3
    assert {o.doc_type for o in outcomes} == {
        "facture_achat", "facture_vente", "devis"
    }
    # Le nom affiche garde la trace de l'archive d'origine.
    assert all("lot.zip" in o.filename for o in outcomes)
    # Deux ecritures comptables, le devis n'en produit aucune.
    assert sum(1 for o in outcomes if o.accounting) == 2


def test_a_zip_never_writes_outside_its_own_documents(worker):
    """Zip-slip, chemin absolu et fichier non PDF sont rejetes, sans bloquer
    le PDF legitime de la meme archive."""
    worker.add_message(
        "m-slip", internal_date=worker.moment(100),
        attachments={"piege.zip": zip_of({
            "../../etc/passwd.pdf": b"%PDF-evasion",
            "/tmp/absolu.pdf": b"%PDF-absolu",
            "notes.txt": b"ceci n'est pas un PDF",
            "achat.pdf": pdf_bytes(ACHAT),
        })},
    )
    summary = worker.process_once()[0]

    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].doc_type == "facture_achat"
    rejected = " ".join(f"{n} {r}" for n, r in summary.rejected)
    assert "passwd" in rejected and "absolu" in rejected and "notes.txt" in rejected


def test_a_zip_bomb_is_refused_before_any_document_is_written(worker):
    worker.add_message(
        "m-bombe", internal_date=worker.moment(200),
        attachments={"bombe.zip": zip_of({"gros.pdf": b"%PDF" + b"0" * 20_000_000})},
    )
    summary = worker.process_once()[0]

    assert summary.outcomes == []
    assert summary.rejected
    assert worker.workbook.writes_to("05_FACTURES_ACHATS") == []


def test_zip_limits_are_configurable(db_path, monkeypatch, worker):
    strict = FakeMailWorker(worker.workbook, db_path, zip_limits=ZipLimits(max_files=1))
    strict.add_message(
        "m-limite", internal_date=strict.moment(300),
        attachments={"lot.zip": zip_of({
            "a.pdf": pdf_bytes(ACHAT), "b.pdf": pdf_bytes(VENTE),
        })},
    )
    summary = strict.process_once()[0]
    assert len(summary.outcomes) <= 1
    assert summary.rejected


# === 15. plusieurs pieces jointes dans un meme email ======================

def test_each_attachment_of_one_email_is_processed_independently(worker):
    worker.add_message(
        "m-multi", internal_date=worker.moment(1000),
        attachments={
            "achat.pdf": pdf_bytes(ACHAT),
            "vente.pdf": pdf_bytes(VENTE),
            "devis.pdf": pdf_bytes(DEVIS),
            "recu.pdf": pdf_bytes(RECU),
        },
    )
    summary = worker.process_once()[0]

    assert len(summary.outcomes) == 4
    assert len({o.doc_key for o in summary.outcomes}) == 4
    assert summary.outcomes[0].tab == "05_FACTURES_ACHATS"
    assert summary.outcomes[1].tab == "04_FACTURES_VENTES"


def test_one_unreadable_attachment_never_blocks_the_others(worker):
    worker.add_message(
        "m-mixte", internal_date=worker.moment(1100),
        attachments={
            "casse.pdf": b"%PDF-inconnu-du-registre",
            "achat.pdf": pdf_bytes(ACHAT),
        },
    )
    summary = worker.process_once()[0]

    assert len(summary.outcomes) == 2
    assert summary.outcomes[0].error
    ok = summary.outcomes[1]
    assert ok.action == ACTION_AUTO and ok.tab == "05_FACTURES_ACHATS"


def test_a_failed_download_is_reported_without_losing_the_other_documents(worker):
    worker.add_message(
        "m-dl", internal_date=worker.moment(1200),
        attachments={"perdu.pdf": pdf_bytes(VENTE), "achat.pdf": pdf_bytes(ACHAT)},
    )
    worker.download_failures.add("m-dl-att-1")
    summary = worker.process_once()[0]

    assert [n for n, _ in summary.rejected] == ["perdu.pdf"]
    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].doc_type == "facture_achat"


def test_the_same_pdf_twice_in_one_email_is_written_once(worker):
    worker.add_message(
        "m-dup", internal_date=worker.moment(1300),
        attachments={"achat.pdf": pdf_bytes(ACHAT), "copie.pdf": pdf_bytes(ACHAT)},
    )
    summary = worker.process_once()[0]

    actions = [o.action for o in summary.outcomes]
    assert actions.count(ACTION_AUTO) == 1
    assert actions.count(ACTION_DUPLICATE) == 1
    assert len(worker.workbook.writes_to("05_FACTURES_ACHATS")) >= 1
    numbers = [r[2] for r in worker.workbook.rows("05_FACTURES_ACHATS")]
    assert numbers.count("FAC-TEST-2026-002") == 1


# === 18. curseur Gmail reellement avancant ================================

def test_the_cursor_is_initialised_at_deployment_time_and_ignores_old_mail(worker):
    floor = store.query_floor(worker.cursor())
    worker.add_message("m-vieux", internal_date=floor - 86_400,
                       attachments={"achat.pdf": pdf_bytes(ACHAT)})

    assert worker.process_once() == []
    assert worker.workbook.writes_to("05_FACTURES_ACHATS") == []


def test_the_cursor_advances_after_a_cycle_and_never_goes_backwards(worker):
    base = store.query_floor(worker.cursor()) + 3_600
    worker.add_message("m-a", internal_date=base, attachments={"achat.pdf": pdf_bytes(ACHAT)})
    worker.process_once()

    after_first = worker.cursor()["last_internal_date"]
    assert after_first >= base

    # Un email plus ancien traite ensuite ne doit pas faire reculer le curseur.
    worker.add_message("m-vieux2", internal_date=base - 60,
                       attachments={"vente.pdf": pdf_bytes(VENTE)})
    worker.process_once()
    assert worker.cursor()["last_internal_date"] >= after_first


def test_the_query_keeps_an_overlap_window_so_no_mail_is_skipped(worker):
    cursor = worker.cursor()
    assert store.query_floor(cursor) == cursor["last_internal_date"] - store.OVERLAP_SECONDS
    assert f"after:{store.query_floor(cursor)}" in worker.effective_query()
    assert worker.query in worker.effective_query()


def test_a_second_cycle_on_the_same_mail_creates_no_second_row(worker):
    base = store.query_floor(worker.cursor()) + 3_600
    worker.add_message("m-rejeu", internal_date=base, attachments={"achat.pdf": pdf_bytes(ACHAT)})
    worker.process_once()
    rows_after_first = len(worker.workbook.rows("05_FACTURES_ACHATS"))

    second = worker.process_once()
    assert rows_after_first == len(worker.workbook.rows("05_FACTURES_ACHATS"))
    for summary in second:
        assert all(o.action == ACTION_DUPLICATE for o in summary.outcomes)


def test_reprocess_rewinds_the_cursor_without_duplicating_anything(worker):
    base = store.query_floor(worker.cursor()) + 3_600
    worker.add_message("m-rep", internal_date=base, attachments={"achat.pdf": pdf_bytes(ACHAT)})
    worker.process_once()
    rows = len(worker.workbook.rows("05_FACTURES_ACHATS"))

    before = worker.cursor()["last_internal_date"]
    worker.rewind(hours=48)
    assert worker.cursor()["last_internal_date"] < before

    worker.process_once()
    assert len(worker.workbook.rows("05_FACTURES_ACHATS")) == rows


def test_two_chat_ids_never_share_a_cursor(worker, db_path):
    other = store.get_or_init_cursor(db_path, 111222333, 1_700_000_000)
    assert other["last_internal_date"] == 1_700_000_000
    assert worker.cursor()["last_internal_date"] != 1_700_000_000


# === garde-fous transverses ==============================================

def test_gmail_is_only_ever_read(worker):
    worker.add_message("m-ro", internal_date=worker.moment(2000),
                       attachments={"achat.pdf": pdf_bytes(ACHAT)})
    worker.process_once()

    used = {slug for slug, _ in worker.gmail_calls}
    assert used <= GMAIL_READ_ONLY, used
    forbidden = ("SEND", "DELETE", "TRASH", "MODIFY", "DRAFT", "REPLY")
    assert not [s for s in used if any(word in s for word in forbidden)]


def test_every_composio_call_is_scoped_to_this_chat(worker):
    assert worker.user_id == USER_ID


def test_the_idempotency_key_binds_chat_message_attachment_and_hash():
    a = idempotency_key(USER_ID, "m1", "att-1", "sha-a")
    assert a != idempotency_key("telegram_42", "m1", "att-1", "sha-a")
    assert a != idempotency_key(USER_ID, "m2", "att-1", "sha-a")
    assert a != idempotency_key(USER_ID, "m1", "att-2", "sha-a")
    assert a != idempotency_key(USER_ID, "m1", "att-1", "sha-b")


def test_the_telegram_summary_carries_no_secret(worker):
    worker.add_message("m-res", internal_date=worker.moment(2100),
                       attachments={"achat.pdf": pdf_bytes(ACHAT), "devis.pdf": pdf_bytes(DEVIS)})
    text = build_summary(worker.process_once()[0])

    assert "Documents trouves" in text
    lowered = text.lower()
    for word in ("api", "token", "secret", "bearer", "x-api-key", "fake"):
        assert word not in lowered


# === regression : la boucle de veille ne doit jamais planter ==============

def test_the_watch_loop_survives_a_full_iteration(monkeypatch):
    """Un cycle complet de la boucle Gmail, jusqu'a la mise en sommeil.

    Le bug corrige ici etait invisible aux tests unitaires : la boucle
    dormait sur un objet supprime (`gmail_watcher`), et le worker mourait
    silencieusement apres son tout premier cycle en production.
    """
    import asyncio

    import app.bot as bot_module

    monkeypatch.setattr(bot_module.settings, "gmail_watch_enabled", True)
    monkeypatch.setattr(bot_module.settings, "gmail_watch_chat_id", CHAT_ID)
    monkeypatch.setattr(
        type(bot_module.mail_worker), "is_configured", property(lambda self: True)
    )
    monkeypatch.setattr(bot_module.mail_worker, "process_once", lambda: [])

    slept: list[float] = []

    class LoopStopped(Exception):
        pass

    async def fake_sleep(seconds):
        slept.append(seconds)
        raise LoopStopped

    monkeypatch.setattr(bot_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(LoopStopped):
        asyncio.run(bot_module._gmail_watch_loop(bot=None))

    assert slept == [bot_module.mail_worker.poll_seconds]
