from contextlib import closing
import json
import sqlite3
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import base64

import pytest

from app import companies as registry, bootstrap, doc_store as store, tenancy
from app.db import init_db
from app.auto_provision import AutoProvisioner, ProvisionDefaults
from app.cycle_health import CycleHealth, read_status
from app.operations import approve, snapshot, add_note, safe_link
from app.operations_web import render, make_server
from app.tenant_worker import TenantWorker
from app.tenant_context import TenantNotWritable
from app.mail_worker import MailWorker, MailWorkerError
from test_bootstrap import FauxSheets, FauxDrive, _inscrire, _lancer


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / 'ops.db')
    init_db(path)
    store.ensure_schema(path)
    tenancy.migrate_to_multi_tenant(path)
    registry.ensure_schema(path)
    return path


def provision(database):
    return AutoProvisioner(database, base_address='operator@example.invalid',
                           sheets=FauxSheets(), drive=FauxDrive(), template_sheet_id='template',
                           defaults=ProvisionDefaults(telegram_chat_id='123'), require_approval=True).provision('new-client')


def config():
    from app.ledger import TEMPLATE_ACCOUNTS
    return dict(legal_name='SOCIETE FICTIVE DEMO SARL', ice='000000000000001', country='MA',
                currency='MAD', allowed_vat_rates=['20'], telegram_chat_id='123',
                account_mapping={k:list(v) for k,v in TEMPLATE_ACCOUNTS.items()})


def claim(database, company, key, state):
    store.claim_document(database, key, 123, gmail_message_id=key, attachment_id=key,
                         file_sha256=key, filename=key+'.pdf', company_id=company)
    store.set_state(database, key, state)


def test_replaying_bootstrap_never_clears_prepared_workbook(database):
    _inscrire(database)
    sheets, drive = FauxSheets(), FauxDrive()
    _lancer(database, sheets, drive)
    first = list(sheets.effacements)
    for _ in range(3):
        result = _lancer(database, sheets, drive)
        assert result.activated
    assert sheets.effacements == first


def test_never_clears_an_operator_supplied_workbook(database):
    _inscrire(database, sheet_id='existing-live-sheet')
    sheets, drive = FauxSheets(), FauxDrive()
    _lancer(database, sheets, drive)
    assert sheets.effacements == []


def test_template_backups_are_cleared_only_in_new_copy(database):
    _inscrire(database)
    backup = '21_A_VERIFIER_BACKUP_2026-08-26T15-54-33+00-00'
    sheets = FauxSheets(list(bootstrap.TRANSACTIONAL_TABS) + [backup])
    result = _lancer(database, sheets, FauxDrive())
    assert (result.sheet_id, f"'{backup}'!A2:Z") in sheets.effacements
    assert all(sheet == result.sheet_id and a1.endswith(':Z') for sheet, a1 in sheets.effacements)
    count = len(sheets.effacements)
    _lancer(database, sheets, FauxDrive())
    assert len(sheets.effacements) == count


@pytest.mark.parametrize('name', ['CLIENT NOVA DEMO SARL', 'CLIENTEL SARL', 'FOURNISSEUR SERVICES SARL'])
def test_legal_names_starting_with_role_words_remain_intact(name):
    from app.doc_extract import clean_party_name, extract_document
    assert clean_party_name(name) == name
    assert clean_party_name('Client : ' + name) == name
    doc = extract_document(['FACTURE DE VENTE\nFournisseur : NOVA-DEMO-2309\nICE : 009999000000023\nClient : ' + name + '\nICE : 009999000000025\nNumero : VTE-NOVA-2026-001\nDate : 22/09/2026\nTotal HT : 2000.00 MAD\nTVA 20 % : 400.00 MAD\nTotal TTC : 2400.00 MAD'])
    assert doc.destinataire == name
    assert doc.destinataire_ice == '009999000000025'


def test_review_tooltip_uses_a_supported_nonblocking_validation(database):
    from app.doc_pipeline import DocumentPipeline
    from test_review_tab_architecture import entree
    from workbook_fake import FakeWorkbook
    gateway = FakeWorkbook()
    pipeline = DocumentPipeline(gateway, db_path=database, chat_id=123, spreadsheet_id='test')
    pipeline._explain_review_row(21, 2, entree())
    rule = gateway.validations[-1]
    assert rule['validation_type'] == 'CUSTOM_FORMULA'
    assert rule['values'] == ['=TRUE'] and rule['strict'] is False
    assert rule['input_message']


def test_interrupted_initialization_can_finish_then_is_never_repeated(database):
    _inscrire(database)
    sheets, drive = FauxSheets(), FauxDrive()
    sheets.echec_effacement_sur = '05_FACTURES_ACHATS'
    with pytest.raises(RuntimeError):
        _lancer(database, sheets, drive)
    assert not registry.get_company(database, 'v2-smoke').bootstrap_initialized
    sheets.echec_effacement_sur = ''
    _lancer(database, sheets, drive)
    assert len(sheets.copies) == 1
    count = len(sheets.effacements)
    _lancer(database, sheets, drive)
    assert len(sheets.effacements) == count


def test_suspended_company_never_reactivated_by_bootstrap(database):
    _inscrire(database, status=registry.SUSPENDED)
    sheets = FauxSheets()
    with pytest.raises(bootstrap.BootstrapError, match='SUSPENDED'):
        _lancer(database, sheets, FauxDrive())
    assert not sheets.copies and not sheets.effacements


def test_copy_returning_template_id_cannot_clear_template(database):
    _inscrire(database)
    sheets = FauxSheets()
    sheets.copy_spreadsheet = lambda source_id, title: source_id
    with pytest.raises(bootstrap.BootstrapError, match='non distincte'):
        _lancer(database, sheets, FauxDrive())
    assert not sheets.effacements


def test_controlled_onboarding_survives_bootstrap_restart(database):
    result = provision(database)
    assert result.created and not result.activated
    company = registry.get_company(database, 'new-client')
    assert company.legal_name == '' and company.approval_required
    registry.update_company(database, 'new-client', legal_name='DECLARED DEMO')
    sheets = FauxSheets()
    bootstrap.bootstrap_company(database, 'new-client', sheets=sheets, drive=FauxDrive(), template_sheet_id='template')
    assert not sheets.effacements
    assert not registry.get_company(database, 'new-client').can_write
    with pytest.raises(registry.CompanyError, match='validation administrateur'):
        registry.set_status(database, 'new-client', registry.ACTIVE)


def test_runtime_provisioner_requires_approval_by_default(database):
    from types import SimpleNamespace
    from app.multitenant_runtime import build_provisioner
    settings = SimpleNamespace(auto_provision_enabled=True, auto_provision_base_address='owner@example.invalid',
                               template_sheet_id='template', db_path=database, gmail_watch_chat_id=123)
    p = build_provisioner(settings, sheets=FauxSheets(), drive=FauxDrive())
    assert not p.provision('new-client').usable


def test_approval_is_explicit_atomic_audited_and_not_repeatable(database):
    provision(database)
    revision = snapshot(database, 'new-client')['companies'][0]['revision']
    approve(database, 'new-client', config(), revision, 'test-operator')
    assert registry.get_company(database, 'new-client').can_write
    view = snapshot(database, 'new-client')['companies'][0]
    assert len(view['audit']) == 1 and view['audit'][0]['actor'] == 'test-operator'
    with pytest.raises(ValueError, match='modifiee'):
        approve(database, 'new-client', config(), revision, 'test-operator')
    assert len(snapshot(database, 'new-client')['companies'][0]['audit']) == 1


@pytest.mark.parametrize('change', [
    {'ice':''}, {'ice':'123'}, {'country':'FR'}, {'currency':'EUR'},
    {'telegram_chat_id':'0'}, {'allowed_vat_rates':[]}, {'allowed_vat_rates':['NaN']},
    {'account_mapping':{}}, {'legal_name':''}, {'account_mapping':{'achat':'6111'}},
])
def test_bad_configuration_never_activates(database, change):
    provision(database)
    data=config(); data.update(change)
    with pytest.raises(ValueError):
        approve(database, 'new-client', data, snapshot(database,'new-client')['companies'][0]['revision'], 'operator')
    assert not registry.get_company(database,'new-client').can_write


def test_approval_rejects_stale_revision(database):
    provision(database)
    revision=snapshot(database,'new-client')['companies'][0]['revision']
    registry.update_company(database, 'new-client', telegram_chat_id='456')
    with pytest.raises(ValueError, match='modifiee'):
        approve(database,'new-client',config(),revision,'operator')


def test_approval_rejects_shared_destinations(database):
    provision(database)
    co=registry.get_company(database,'new-client')
    _inscrire(database, 'another-client', sheet_id=co.sheet_id)
    with pytest.raises(ValueError, match='partagees'):
        approve(database,'new-client',config(),snapshot(database,'new-client')['companies'][0]['revision'],'operator')


def test_snapshot_and_notes_are_scoped_even_with_shared_chat(database):
    _inscrire(database, 'first-company')
    _inscrire(database, 'second-company')
    claim(database,'first-company','first-doc',store.NEEDS_REVIEW)
    claim(database,'second-company','private-doc',store.NEEDS_REVIEW)
    before = store.get_document(database,'first-doc')
    add_note(database,'first-company','first-doc','Demander une photo lisible.','operator')
    assert store.get_document(database,'first-doc') == before
    data=snapshot(database,'first-company')
    assert 'private-doc' not in json.dumps(data)
    with pytest.raises(ValueError):
        add_note(database,'first-company','private-doc','Cross-tenant note','operator')
    assert len(data['companies'][0]['audit']) == 1


@pytest.mark.parametrize('state,listing', [(store.NEEDS_REVIEW,store.list_pending_review),(store.PARTIAL,store.list_unfinished)])
def test_resume_lists_do_not_mix_companies(database, state, listing):
    claim(database,'first-company','first-doc',state)
    claim(database,'second-company','private-doc',state)
    assert [r['doc_key'] for r in listing(database,123,company_id='first-company')] == ['first-doc']


def test_resume_guard_precedes_any_network_or_materialization(database, monkeypatch):
    worker=MailWorker(api_key='fake',chat_id=123,db_path=database,company_id='first-company')
    monkeypatch.setattr(worker,'materialize',lambda row: pytest.fail('cross-tenant materialization'))
    with pytest.raises(MailWorkerError,match='autre entreprise'):
        worker.resume({'company_id':'second-company','chat_id':'123'})


def test_notification_guard_rejects_another_company(database):
    from app.doc_pipeline import DocumentOutcome
    claim(database, 'second-company', 'private-doc', store.NEEDS_REVIEW)
    before = store.get_document(database, 'private-doc')
    worker = MailWorker(api_key='fake', chat_id=123, db_path=database, company_id='first-company')
    with pytest.raises(MailWorkerError, match='autre entreprise'):
        worker.mark_notified(DocumentOutcome(doc_key='private-doc', filename='private.pdf'))
    assert store.get_document(database, 'private-doc') == before


@pytest.mark.asyncio
async def test_delivery_uses_company_destination_and_company_owner(monkeypatch):
    from types import SimpleNamespace
    from app import bot as module
    from test_gmail_loop_delivery import ResumeFactice, Resultat, Envoi
    chats, marked = [], []
    async def send(*, chat_id, text):
        chats.append(chat_id)
        return Envoi(len(chats))
    monkeypatch.setattr(module.settings, 'gmail_watch_chat_id', 42)
    monkeypatch.setattr(module.mail_worker, 'mark_notified', lambda *a, **kw: pytest.fail('legacy owner used'))
    owner = SimpleNamespace(mark_notified=lambda outcome, **kw: marked.append(outcome.doc_key))
    result = await module.deliver_summary(SimpleNamespace(send_message=send),
        ResumeFactice([Resultat('imported')], [Resultat('review', 'review')]), owner=owner, chat_id=987)
    assert result['summary_delivered'] and not result['failed']
    assert chats == [987, 987] and set(marked) == {'imported', 'review'}


def test_cached_worker_cannot_bypass_suspension(database):
    _inscrire(database, 'first-company', status=registry.ACTIVE, sheet_id='s',drive_folder_id='d')
    worker=TenantWorker(api_key='fake',chat_id=123,db_path=database,query='in:inbox')
    worker.worker_for('first-company')
    registry.set_status(database,'first-company',registry.SUSPENDED)
    with pytest.raises(TenantNotWritable):
        worker.worker_for('first-company')


def test_snapshot_and_html_escape_untrusted_documents(database):
    _inscrire(database,'first-company')
    claim(database,'first-company','doc',store.NEEDS_REVIEW)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute('UPDATE documents SET filename=? WHERE doc_key=?', ('<script>alert(1)</script>','doc'))
    store.update_document(database,'doc',filename='<script>alert(1)</script>',
                          drive_link='javascript:alert(1)',payload=json.dumps({'reasons':['<img src=x onerror=alert(1)>']}))
    text=render(snapshot(database,'first-company'))
    assert '<script>' not in text and '<img src=x' not in text and 'javascript:' not in text
    assert '&lt;script&gt;' in text


def test_snapshot_of_missing_db_does_not_create_it(tmp_path):
    file=tmp_path/'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        snapshot(str(file))
    assert not file.exists()


@pytest.mark.parametrize('url',['http://drive.google.com/x','javascript:alert(1)','https://drive.google.com.evil/x','https://evil@drive.google.com/x'])
def test_unsafe_links_are_not_clickable(url):
    assert safe_link(url) == ''


def test_health_transitions_are_durable_bounded_and_show_staleness(database):
    health=CycleHealth(database,60)
    assert health.finish(False,now=100)==''
    assert health.finish(False,now=101)==''
    assert health.finish(False,now=102)=='failure'
    restart=CycleHealth(database,60)
    assert restart.finish(False,now=103)==''
    assert restart.finish(True,now=104)=='recovered'
    assert restart.finish(True,now=105)==''
    assert read_status(database,now=106)['status']=='ok'
    assert read_status(database,now=400)['status']=='stale'


def test_console_requires_auth_and_cannot_write(database):
    _inscrire(database,'first-company')
    token='a-long-test-only-token-not-a-real-secret'
    server=make_server(database,'first-company',0,token)
    thread=Thread(target=server.serve_forever,daemon=True); thread.start()
    url=f'http://127.0.0.1:{server.server_port}/'
    try:
        with pytest.raises(HTTPError) as denied:
            urlopen(url)
        assert denied.value.code==401
        auth='Basic '+base64.b64encode(('operator:'+token).encode()).decode()
        with urlopen(Request(url,headers={'Authorization':auth})) as response:
            assert response.status==200 and 'no-store' in response.headers['Cache-Control']
            assert b'first-company' in response.read()
        with pytest.raises(HTTPError) as refused:
            urlopen(Request(url,data=b'{}',headers={'Authorization':auth},method='POST'))
        assert refused.value.code==405
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
