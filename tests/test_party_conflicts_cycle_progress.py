import pytest

from app import doc_store as store
from app.cycle_health import CycleHealth, read_status
from app.doc_policy import ACTION_REVIEW
from test_new_row_highlight import db_path, workbook, pipeline, run, ACHAT


def test_same_ice_different_name_is_parked_without_changing_reference(pipeline, workbook, db_path):
    row = next(r for r in workbook.tabs['03_FOURNISSEURS'] if len(r) > 2 and r[2] == '002345678000043')
    row[1] = 'BUREAU SARL'
    before = list(row)
    purchases = len(workbook.tabs['05_FACTURES_ACHATS'])
    result = run(pipeline, ACHAT)
    assert result.action == ACTION_REVIEW
    assert 'nom du tiers different' in ' '.join(result.reasons)
    assert len(workbook.tabs['05_FACTURES_ACHATS']) == purchases
    assert row == before
    assert store.get_document(db_path, result.doc_key)['state'] == store.NEEDS_REVIEW


@pytest.mark.parametrize('tab', ['02_CLIENTS', '03_FOURNISSEURS'])
def test_legacy_truncated_party_name_is_not_silently_reused(pipeline, workbook, tab):
    workbook.tabs[tab].append(['TEST-001', 'RIF DEMO SARL', '009999000000035'])
    match = pipeline.resolve_party(tab, '009999000000035', 'CLIENT RIF DEMO SARL')
    assert match.ambiguous
    assert 'TEST-001' in match.reason


def test_normalized_matching_name_does_not_require_review(pipeline):
    match = pipeline.resolve_party('03_FOURNISSEURS', '002345678000043', 'atlas bureau sarl')
    assert match.existing and not match.ambiguous


def test_active_cycle_does_not_report_success_or_stale_after_three_minutes(tmp_path):
    db = str(tmp_path / 'agent.db')
    health = CycleHealth(db, 60)
    health.finish(True, now=100)
    health.start(now=160)
    status = read_status(db, now=600)
    assert status['status'] == 'processing'
    assert status['last_success'] == 100
    assert status['elapsed_seconds'] == 440
    assert read_status(db, now=1961)['status'] == 'stalled'
    assert CycleHealth(db, 60).last_success == 100


def test_start_does_not_reset_failures_or_hide_recovery(tmp_path):
    db = str(tmp_path / 'agent.db')
    health = CycleHealth(db, 60)
    for n in range(3):
        health.finish(False, now=100+n)
    health.start(now=110)
    assert read_status(db, now=111)['consecutive_failures'] == 3
    assert health.finish(True, now=120) == 'recovered'
    assert read_status(db, now=121)['status'] == 'ok'
    assert 'started_at' not in read_status(db, now=121)


@pytest.mark.asyncio
@pytest.mark.parametrize('multi', [False, True])
async def test_gmail_loop_records_active_work_before_completion(tmp_path, monkeypatch, multi):
    import asyncio
    from types import SimpleNamespace
    from app import bot
    db = str(tmp_path / 'live.db')
    seen = []
    def process():
        seen.append(read_status(db)['status'])
        return SimpleNamespace(emails=[], quarantined=[], technical_failures=0) if multi else []
    worker = SimpleNamespace(is_configured=True, user_id='test', poll_seconds=60,
                             query='test', process_once=process)
    monkeypatch.setattr(bot, 'mail_worker', worker)
    monkeypatch.setattr(bot, '_build_multitenant_worker', lambda: worker)
    monkeypatch.setattr(bot.settings, 'gmail_watch_enabled', True)
    monkeypatch.setattr(bot.settings, 'multi_tenant_enabled', multi)
    monkeypatch.setattr(bot.settings, 'db_path', db)
    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(bot.asyncio, 'sleep', stop)
    with pytest.raises(asyncio.CancelledError):
        await bot._gmail_watch_loop(SimpleNamespace())
    assert seen == ['processing']
    assert read_status(db)['status'] == 'ok'
