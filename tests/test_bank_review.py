import json
import sqlite3
from decimal import Decimal

import pytest

from app import bank_review as br, ledger
from app.db import init_db
from app.doc_pipeline import DocumentPipeline
from tests.workbook_fake import FakeWorkbook


def invoice(key='FA-1', ref='F-1', total='1200', paid='0', party='FRS-1'):
    return [key, '2026-10-07', ref, party, 'Fournisseur', '', '', '', '', total, '', '', '', '', paid, '', '']


def bank(key='TX-1', ref='F-1', debit='500', credit=''):
    return [key, 'Banque', '2026-10-07', '', 'Virement', ref, debit, credit, '', '', '', '', 'Non rapproche']


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path/'bank.db')
    init_db(path)
    book = FakeWorkbook()
    pipe = DocumentPipeline(book, db_path=path, chat_id=123, spreadsheet_id='sheet', company_id='co',
                            account_mapping={'banque': ['5141', 'Banque']})
    pipe._write(f'{br.PURCHASES}!A2:Q2', [invoice()])
    pipe._write(f'{br.SALES}!A2:Q2', [invoice('FV-1', 'V-1', party='CLI-1')])
    pipe._write(f'{br.BANK}!A2:M2', [bank()])
    return pipe, book


def approve_first(pipe, bank_id='TX-1'):
    record = next(r for r in br.records(pipe._db, 'co', 'sheet') if r['bank_id']==bank_id)
    br.decide(pipe._db, 'co', bank_id, record['revision'], 'approve', 'Comptable', 'Justificatif controle')
    return record


def test_partial_requires_approval_and_replay_is_idempotent(setup):
    pipe, book = setup
    br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14] == '0'
    approve_first(pipe)
    br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14:16] == [500.0, 'Partiellement payee']
    before = ledger.entries_for(pipe._db, 'co')
    journal = list(book.rows('12_JOURNAL_COMPTABLE'))
    br.refresh(pipe)
    assert ledger.entries_for(pipe._db, 'co') == before
    assert book.rows('12_JOURNAL_COMPTABLE') == journal
    assert sum(Decimal(r['debit']) for r in before)==Decimal('500')
    assert sum(Decimal(r['credit']) for r in before)==Decimal('500')


def test_second_payment_accumulates_and_has_distinct_piece(setup):
    pipe, book = setup
    br.refresh(pipe); approve_first(pipe); br.refresh(pipe)
    pipe._write(f'{br.BANK}!A3:M3', [bank('TX-2', debit='700')])
    br.refresh(pipe); approve_first(pipe, 'TX-2'); br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14:16] == [1200.0, 'Payee']
    assert len({r['piece'] for r in ledger.entries_for(pipe._db, 'co')})==2


def test_grouped_explicit_references(setup):
    pipe, book = setup
    pipe._write(f'{br.PURCHASES}!A3:Q3', [invoice('FA-2', 'F-2', '600')])
    pipe._write(f'{br.BANK}!A2:M2', [bank(ref='F-1; F-2', debit='1800')])
    br.refresh(pipe); approve_first(pipe); br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14:16] == [1200.0, 'Payee']
    assert book.row(br.PURCHASES, 3)[14:16] == [600.0, 'Payee']
    assert book.row(br.BANK, 2)[11]=='F-1; F-2'


def test_missing_invoice_reconsidered_when_it_arrives(setup):
    pipe, _ = setup
    pipe._write(f'{br.BANK}!A2:M2', [bank(ref='F-2')])
    br.refresh(pipe)
    old = br.records(pipe._db, 'co', 'sheet')[0]
    assert not json.loads(old['payload'])['allocations']
    pipe._write(f'{br.PURCHASES}!A3:Q3', [invoice('FA-2', 'F-2')])
    br.refresh(pipe)
    new = br.records(pipe._db, 'co', 'sheet')[0]
    assert json.loads(new['payload'])['allocations']
    assert new['revision']!=old['revision']
    with pytest.raises(ValueError):
        br.decide(pipe._db, 'co', 'TX-1', old['revision'], 'approve', 'Comptable', 'Ancienne revue')


def test_live_balance_change_invalidates_approval(setup):
    pipe, book = setup
    br.refresh(pipe); approve_first(pipe)
    pipe._write(f'{br.PURCHASES}!O2:P2', [[100, 'Partiellement payee']])
    br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14]==100
    assert not ledger.entries_for(pipe._db, 'co')
    assert br.records(pipe._db, 'co', 'sheet')[0]['status']=='pending'


@pytest.mark.parametrize('inv,op', [
    (invoice(), bank(debit='1500')),
    (invoice(paid='1200'), bank()),
    (invoice(), bank(debit='500', credit='500')),
    (invoice(), bank(ref='', debit='500')),
    (invoice(), bank(debit='NaN')),
    (invoice(), bank(debit='0')),
    (invoice(), bank(debit='0.001')),
    (invoice(), bank(ref='F-1; F-1')),
])
def test_unsafe_operations_have_no_allocation(inv, op):
    assert not br.proposal(op, [inv], br.PURCHASES)['allocations']


def test_duplicate_invoice_references_and_mixed_parties_are_ambiguous():
    assert not br.proposal(bank(), [invoice(), invoice('FA-2')], br.PURCHASES)['allocations']
    assert not br.proposal(bank(ref='F-1; F-2', debit='1800'),
        [invoice(), invoice('FA-2', 'F-2', '600', party='FRS-2')], br.PURCHASES)['allocations']


def test_credit_matches_sales_only(setup):
    pipe, book = setup
    pipe._write(f'{br.BANK}!A2:M2', [bank(ref='F-1', debit='', credit='500')])
    br.refresh(pipe)
    assert not json.loads(br.records(pipe._db, 'co', 'sheet')[0]['payload'])['allocations']
    pipe._write(f'{br.BANK}!A2:M2', [bank(ref='V-1', debit='', credit='500')])
    br.refresh(pipe); approve_first(pipe); br.refresh(pipe)
    assert book.row(br.SALES, 2)[14]==500.0
    assert book.row(br.PURCHASES, 2)[14]=='0'


def test_repeated_bank_operations_block_approval(setup):
    pipe, _ = setup
    pipe._write(f'{br.BANK}!A3:M3', [bank('TX-2')])
    br.refresh(pipe)
    with pytest.raises(ValueError): approve_first(pipe)


def test_wrong_company_and_rejection_cannot_write(setup):
    pipe, book = setup
    br.refresh(pipe)
    r = br.records(pipe._db, 'co', 'sheet')[0]
    with pytest.raises(ValueError):
        br.decide(pipe._db, 'other', 'TX-1', r['revision'], 'approve', 'A', 'Note')
    br.decide(pipe._db, 'co', 'TX-1', r['revision'], 'reject', 'A', 'Piece incorrecte')
    br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14]=='0'
    with sqlite3.connect(pipe._db) as c:
        assert c.execute('SELECT decision,note FROM bank_review_audit').fetchall()==[('reject', 'Piece incorrecte')]


def test_crash_after_invoice_write_resumes_absolute_target(setup, monkeypatch):
    pipe, book = setup
    br.refresh(pipe); approve_first(pipe)
    original = pipe._append_journal
    monkeypatch.setattr(pipe, '_append_journal', lambda _: (_ for _ in ()).throw(RuntimeError('offline')))
    with pytest.raises(RuntimeError): br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14]==500.0
    monkeypatch.setattr(pipe, '_append_journal', original)
    br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14]==500.0
    assert len(ledger.entries_for(pipe._db, 'co'))==2
    assert br.records(pipe._db, 'co', 'sheet')[0]['status']=='applied'


def test_missing_bank_account_prevents_any_payment_write(setup):
    pipe, book = setup
    pipe._account_mapping = {}
    br.refresh(pipe); approve_first(pipe)
    with pytest.raises(ValueError): br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14]=='0'


def test_feature_disabled_by_default(setup, monkeypatch):
    pipe, _ = setup
    monkeypatch.delenv('BANK_REVIEW_ENABLED', raising=False)
    pipe.refresh_bank_review()
    with sqlite3.connect(pipe._db) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='bank_reviews'").fetchone()


def test_bank_change_after_partial_write_preserves_frozen_plan(setup, monkeypatch):
    pipe, book = setup
    br.refresh(pipe); approve_first(pipe)
    original = pipe._append_journal
    monkeypatch.setattr(pipe, '_append_journal', lambda _: (_ for _ in ()).throw(RuntimeError('offline')))
    with pytest.raises(RuntimeError): br.refresh(pipe)
    pipe._write(f'{br.BANK}!G2:G2', [['600']])
    monkeypatch.setattr(pipe, '_append_journal', original)
    br.refresh(pipe)
    record = br.records(pipe._db, 'co', 'sheet')[0]
    assert record['status']=='conflict'
    assert json.loads(record['payload'])['allocations'][0]['amount']=='500'
    assert book.row(br.PURCHASES, 2)[14]==500.0


def test_another_approved_payment_is_revalidated_against_first(setup):
    pipe, book = setup
    pipe._write(f'{br.BANK}!A3:M3', [bank('TX-2', debit='700')])
    br.refresh(pipe); approve_first(pipe); approve_first(pipe, 'TX-2')
    br.refresh(pipe)
    assert book.row(br.PURCHASES, 2)[14]==500.0
    record = next(r for r in br.records(pipe._db, 'co', 'sheet') if r['bank_id']=='TX-2')
    assert record['status']=='pending'
    assert json.loads(record['payload'])['allocations'][0]['before']=='500.0'


def test_authenticated_browser_bank_decision_and_csrf_scope(setup, monkeypatch):
    import base64
    import re
    from threading import Thread
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from urllib.parse import urlencode
    from app import companies
    from app.operations_web import make_server
    pipe, _ = setup
    companies.ensure_schema(pipe._db)
    companies.register_company(pipe._db, 'co', status=companies.ACTIVE, sheet_id='sheet')
    companies.register_company(pipe._db, 'other', status=companies.ACTIVE, sheet_id='other-sheet')
    br.refresh(pipe)
    monkeypatch.setenv('BANK_REVIEW_ENABLED', 'true')
    token='test-only-operations-token-long-enough'
    server=make_server(pipe._db, 'co', 0, token)
    thread=Thread(target=server.serve_forever, daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    auth='Basic '+base64.b64encode(('operator:'+token).encode()).decode()
    headers={'Authorization':auth, 'Content-Type':'application/x-www-form-urlencoded'}
    try:
        with urlopen(Request(url+'/', headers=headers), timeout=5) as response:
            html=response.read().decode()
        csrf=re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        revision=br.records(pipe._db, 'co', 'sheet')[0]['revision']
        values=dict(csrf=csrf, company='co', bank='TX-1', revision=revision, note='Justificatif verifie', decision='approve')
        for fields, used_headers, expected in [
            ({**values,'csrf':'invalid'}, headers, 403),
            ({**values,'company':'other'}, headers, 403),
            (values, {'Content-Type':'application/x-www-form-urlencoded'}, 401),
        ]:
            with pytest.raises(HTTPError) as error:
                urlopen(Request(url+'/bank-decision', data=urlencode(fields).encode(),headers=used_headers),timeout=5)
            assert error.value.code==expected
        with urlopen(Request(url+'/bank-decision',data=urlencode(values).encode(),headers=headers),timeout=5) as response:
            assert response.status==200
        assert br.records(pipe._db, 'co', 'sheet')[0]['status']=='approved'
        with pytest.raises(HTTPError) as replay:
            urlopen(Request(url+'/bank-decision',data=urlencode(values).encode(),headers=headers),timeout=5)
        assert replay.value.code==409
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
