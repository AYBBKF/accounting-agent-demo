"""Detection des emails : PDF seul, ZIP seul, ou les deux.

Le pipeline savait ouvrir les archives ZIP depuis le debut, mais la requete
Gmail exigeait `filename:pdf`. Un email ne portant que le pack ZIP n'etait
donc JAMAIS ramene par le worker : le defaut n'etait pas dans le traitement,
il etait en amont, dans la selection.

Ce fichier ne se contente pas de verifier la chaine de caracteres de la
requete : il l'INTERPRETE (sous-ensemble reellement utilise de la syntaxe
Gmail) et fait tourner un vrai cycle du worker sur des emails simules, pour
prouver que les trois formes d'envoi arrivent bien jusqu'au pipeline.
"""
import re
import time

import pytest

from app.config import Settings
from app.doc_extract import extract_document
from app.doc_policy import ACTION_AUTO
from test_mail_worker import ACHAT, DEVIS, FakeMailWorker, pdf_bytes, text_of, zip_of
from workbook_fake import FakeWorkbook

QUERY = Settings(_env_file=None).gmail_watch_query


def gmail_query_matches(query: str, filenames: list[str]) -> bool:
    """Interprete le sous-ensemble de syntaxe Gmail que nous utilisons.

    - `has:attachment` exige au moins une piece jointe ;
    - `{a b}` est un OU entre ses termes ;
    - `filename:ext` hors accolade est une exigence ferme (ET) ;
    - `in:inbox` ne joue aucun role ici.
    """
    def filename_matches(term: str) -> bool:
        extension = term.split(":", 1)[1].lower()
        return any(name.lower().endswith(f".{extension}") for name in filenames)

    remainder = query
    for group in re.findall(r"\{([^}]*)\}", query):
        terms = [t for t in group.split() if t.startswith("filename:")]
        if terms and not any(filename_matches(t) for t in terms):
            return False
        remainder = remainder.replace("{" + group + "}", " ")

    for token in remainder.split():
        if token == "has:attachment" and not filenames:
            return False
        if token.startswith("filename:") and not filename_matches(token):
            return False
    return True


# --- l'interprete lui-meme, pour que le test ne se trompe pas de verite ---

def test_the_query_interpreter_behaves_like_gmail_on_a_conjunction():
    """Deux `filename:` SANS accolade forment un ET : c'etait le piege."""
    conjunction = "in:inbox has:attachment filename:pdf filename:zip"
    assert not gmail_query_matches(conjunction, ["pack.zip"])
    assert not gmail_query_matches(conjunction, ["facture.pdf"])
    assert gmail_query_matches(conjunction, ["facture.pdf", "pack.zip"])


def test_the_query_interpreter_treats_braces_as_a_disjunction():
    assert gmail_query_matches("{filename:pdf filename:zip}", ["pack.zip"])
    assert gmail_query_matches("{filename:pdf filename:zip}", ["facture.pdf"])
    assert not gmail_query_matches("{filename:pdf filename:zip}", ["note.txt"])


# --- la requete reellement deployee --------------------------------------

@pytest.mark.parametrize(
    "filenames,expected",
    [
        (["facture.pdf"], True),                       # PDF seul
        (["pack.zip"], True),                          # ZIP seul
        (["facture.pdf", "pack.zip"], True),           # les deux
        (["a.pdf", "b.pdf", "lot.zip"], True),         # plusieurs pieces
        (["PACK.ZIP"], True),                          # extension en majuscules
        (["facture.png"], True),                       # facture photographiee PNG
        (["photo.jpg"], True),                         # facture photographiee JPG
        (["scan.jpeg"], True),                         # facture photographiee JPEG
        (["FACTURE.JPG"], True),                       # image en majuscules
        (["achat.pdf", "recu.jpg"], True),             # PDF + photo dans un email
        (["note.txt"], False),                         # rien d'exploitable
        (["logo.gif"], False),                         # image non prise en charge
        ([], False),                                   # aucune piece jointe
    ],
)
def test_the_deployed_query_selects_pdf_zip_image_or_any_mix(filenames, expected):
    assert gmail_query_matches(QUERY, filenames) is expected


def test_the_old_query_would_have_missed_a_photographed_invoice():
    """Garde-fou : une facture photographiee (PNG/JPG) doit desormais passer,
    la ou l'ancienne requete PDF/ZIP seule la laissait invisible."""
    ancienne = "in:inbox has:attachment {filename:pdf filename:zip}"
    assert not gmail_query_matches(ancienne, ["facture.jpg"])
    assert not gmail_query_matches(ancienne, ["facture.png"])
    assert gmail_query_matches(QUERY, ["facture.jpg"])
    assert gmail_query_matches(QUERY, ["facture.png"])
    assert gmail_query_matches(QUERY, ["facture.jpeg"])


def test_the_old_query_would_have_missed_a_zip_only_email():
    """Garde-fou historique : le defaut corrige ici ne doit pas revenir."""
    assert not gmail_query_matches("in:inbox has:attachment filename:pdf", ["pack.zip"])
    assert gmail_query_matches(QUERY, ["pack.zip"])


# --- un vrai cycle du worker sur des emails simules -----------------------

class QueryAwareWorker(FakeMailWorker):
    """Faux Gmail qui applique reellement la requete envoyee par le worker."""

    def _gmail(self, slug, arguments):
        if slug == "GMAIL_FETCH_EMAILS":
            query = str(arguments["query"])
            floor = int(query.rsplit("after:", 1)[1])
            found = []
            for mid, msg in sorted(self.messages.items()):
                if int(msg["internalDate"]) // 1000 < floor:
                    continue
                names = [a["filename"] for a in msg["attachmentList"]]
                if gmail_query_matches(query, names):
                    found.append({"messageId": mid})
            return {"messages": found[: arguments.get("max_results", 5)]}
        return super()._gmail(slug, arguments)


@pytest.fixture
def worker(db_path_for_query, monkeypatch):
    import app.doc_pipeline as module

    registry = {
        pdf_bytes(ACHAT): text_of(ACHAT),
        pdf_bytes(DEVIS): text_of(DEVIS),
    }
    monkeypatch.setattr(
        module, "extract_from_pdf_bytes",
        lambda c, company="X BLASTE", ocr=True: extract_document(
            [registry[c]], company=company
        ),
    )
    built = QueryAwareWorker(FakeWorkbook(), db_path_for_query, query=QUERY)
    return built


@pytest.fixture
def db_path_for_query():
    import tempfile
    from pathlib import Path

    from app import doc_store as store
    from app.db import init_db

    path = tempfile.mktemp(suffix=".db")
    init_db(path)
    store.ensure_schema(path)
    yield path
    Path(path).unlink(missing_ok=True)


def test_an_email_carrying_only_a_zip_reaches_the_pipeline(worker):
    worker.add_message(
        "m-zip-seul", internal_date=int(time.time()) + 3_600,
        attachments={"pack.zip": zip_of({"achat.pdf": pdf_bytes(ACHAT)})},
    )
    summary = worker.process_once()[0]
    assert [o.action for o in summary.outcomes] == [ACTION_AUTO]
    assert summary.outcomes[0].doc_type == "facture_achat"


def test_an_email_carrying_only_pdfs_still_reaches_the_pipeline(worker):
    worker.add_message(
        "m-pdf-seul", internal_date=int(time.time()) + 3_600,
        attachments={"achat.pdf": pdf_bytes(ACHAT)},
    )
    summary = worker.process_once()[0]
    assert [o.action for o in summary.outcomes] == [ACTION_AUTO]


def test_an_email_carrying_both_reaches_the_pipeline_with_every_document(worker):
    worker.add_message(
        "m-mixte", internal_date=int(time.time()) + 3_600,
        attachments={
            "achat.pdf": pdf_bytes(ACHAT),
            "pack.zip": zip_of({"devis.pdf": pdf_bytes(DEVIS)}),
        },
    )
    summary = worker.process_once()[0]
    assert {o.doc_type for o in summary.outcomes} == {"facture_achat", "devis"}


def test_an_email_without_any_usable_attachment_is_never_fetched(worker):
    worker.add_message(
        "m-texte", internal_date=int(time.time()) + 3_600,
        attachments={"note.txt": b"ceci n'est pas un document comptable"},
    )
    assert worker.process_once() == []
    assert worker.workbook.writes_to("05_FACTURES_ACHATS") == []
