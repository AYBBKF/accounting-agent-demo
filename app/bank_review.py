"""Bank allocations proposed from explicit references, approved by an operator.

No subset-sum guessing, fee deduction, currency conversion or invoice overwrite.
The persisted plan contains absolute targets so interrupted writes can resume.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import sqlite3

from app import ledger

PURCHASES = '05_FACTURES_ACHATS'
SALES = '04_FACTURES_VENTES'
BANK = '06_RELEVE_BANCAIRE'
LIMIT = 5000


def enabled():
    return os.getenv('BANK_REVIEW_ENABLED', 'false').strip().lower() == 'true'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def money(value):
    try:
        amount = Decimal(str(value).replace('\u202f', '').replace(' ', '').replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError('Montant absent ou invalide') from exc
    if not amount.is_finite() or amount != amount.quantize(Decimal('.01')):
        raise ValueError('Montant invalide ou precision superieure au centime')
    return amount


def cell(row, index):
    return row[index] if len(row) > index else ''


def identity(row):
    # Ignore bank links/status; the source operation must remain identical.
    return [str(cell(row, i)) for i in range(11)]


def invoice_identity(row):
    return [str(cell(row, i)) for i in (0, 2, 3, 9)]


def ensure_schema(path):
    with closing(sqlite3.connect(path)) as c, c:
        c.execute('''CREATE TABLE IF NOT EXISTS bank_reviews (
          company_id TEXT NOT NULL, sheet_id TEXT NOT NULL, bank_id TEXT NOT NULL,
          revision TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          PRIMARY KEY(company_id,sheet_id,bank_id))''')
        if 'last_error' not in {r[1] for r in c.execute('PRAGMA table_info(bank_reviews)')}:
            c.execute("ALTER TABLE bank_reviews ADD COLUMN last_error TEXT NOT NULL DEFAULT ''")
        c.execute('''CREATE TABLE IF NOT EXISTS bank_review_audit (
          id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, sheet_id TEXT NOT NULL,
          bank_id TEXT NOT NULL, revision TEXT NOT NULL, actor TEXT NOT NULL,
          decision TEXT NOT NULL, note TEXT NOT NULL, payload TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')


def save(path, company, sheet, bank_id, payload):
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    revision = digest(payload)
    ensure_schema(path)
    with closing(sqlite3.connect(path)) as c, c:
        previous = c.execute('SELECT revision,status FROM bank_reviews WHERE company_id=? AND sheet_id=? AND bank_id=?',
                             (company, sheet, bank_id)).fetchone()
        if previous and (previous[0] == revision or previous[1] in ('approved', 'applying', 'applied', 'conflict')):
            return
        c.execute('''INSERT INTO bank_reviews(company_id,sheet_id,bank_id,revision,payload,status)
          VALUES(?,?,?,?,?,'pending') ON CONFLICT(company_id,sheet_id,bank_id) DO UPDATE SET
          revision=excluded.revision,payload=excluded.payload,status='pending',actor='',note='',last_error='',
          updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')''', (company, sheet, bank_id, revision, encoded))


def decide(path, company, bank_id, revision, decision, actor, note):
    if decision not in ('approve', 'reject') or not actor.strip() or not 1 <= len(note.strip()) <= 2000:
        raise ValueError('Decision, acteur et motif explicites requis')
    with closing(sqlite3.connect(path)) as c, c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute('SELECT sheet_id,payload FROM bank_reviews WHERE company_id=? AND bank_id=? AND revision=? AND status=?',
                         (company, bank_id, revision, 'pending')).fetchall()
        if len(rows) != 1:
            raise ValueError('Proposition absente, modifiee ou deja decidee : actualiser la revue')
        if decision == 'approve' and not json.loads(rows[0][1]).get('allocations'):
            raise ValueError('Aucune allocation sure : completer les justificatifs avant approbation')
        c.execute('UPDATE bank_reviews SET status=?,actor=?,note=?,updated_at=strftime(\'%Y-%m-%dT%H:%M:%fZ\',\'now\') WHERE company_id=? AND sheet_id=? AND bank_id=?',
                  ('approved' if decision == 'approve' else 'rejected', actor.strip(), note.strip(), company, rows[0][0], bank_id))
        c.execute('INSERT INTO bank_review_audit(company_id,sheet_id,bank_id,revision,actor,decision,note,payload) VALUES(?,?,?,?,?,?,?,?)',
                  (company, rows[0][0], bank_id, revision, actor.strip(), decision, note.strip(), rows[0][1]))


def records(path, company, sheet):
    ensure_schema(path)
    with closing(sqlite3.connect(path)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute('SELECT * FROM bank_reviews WHERE company_id=? AND sheet_id=? ORDER BY bank_id', (company, sheet))]


def set_status(pipe, bank_id, status):
    with closing(sqlite3.connect(pipe._db)) as c, c:
        c.execute('UPDATE bank_reviews SET status=?,last_error=\'\',updated_at=strftime(\'%Y-%m-%dT%H:%M:%fZ\',\'now\') WHERE company_id=? AND sheet_id=? AND bank_id=?',
                  (status, pipe.company_id, pipe._sheet, bank_id))


def read_tables(pipe):
    data = {tab: pipe._read(f'{tab}!A2:{last}{LIMIT + 1}')
            for tab, last in ((BANK, 'M'), (PURCHASES, 'Q'), (SALES, 'Q'))}
    if any(len(rows) >= LIMIT for rows in data.values()):
        raise ValueError('Limite de lecture atteinte : revue bancaire manuelle requise')
    return data


def proposal(bank, invoices, tab):
    references = [x.strip().casefold() for x in re.split(r'[;,|\n]+', str(cell(bank, 5))) if x.strip()]
    result = {'bank': identity(bank), 'reason': '', 'allocations': []}
    if not references or len(references) != len(set(references)):
        result['reason'] = 'Reference absente ou repetee'
        return result
    try:
        debit, credit = cell(bank, 6), cell(bank, 7)
        if (debit not in ('', None)) == (credit not in ('', None)):
            raise ValueError('Sens bancaire ambigu')
        amount = money(debit if debit not in ('', None) else credit)
        if amount <= 0:
            raise ValueError('Montant bancaire non positif')
        allocations = []
        for ref in references:
            matches = [row for row in invoices if str(cell(row, 2)).strip().casefold() == ref]
            if len(matches) != 1:
                raise ValueError('Facture absente ou reference ambigue : ' + ref)
            inv = matches[0]
            total = money(cell(inv, 9))
            paid = money(cell(inv, 14) or '0')
            remaining = total - paid
            if remaining <= 0 or paid < 0:
                raise ValueError('Facture deja soldee ou montant paye incoherent : ' + ref)
            allocated = amount if len(references) == 1 else remaining
            if allocated > remaining:
                raise ValueError('Montant superieur au reste a payer : ' + ref)
            allocations.append({'tab': tab, 'invoice': invoice_identity(inv), 'before': str(paid),
                                'amount': str(allocated), 'target': str(paid + allocated), 'total': str(total)})
        if len({a['invoice'][2] for a in allocations}) != 1 or not allocations[0]['invoice'][2]:
            raise ValueError('Plusieurs tiers ou tiers absent : repartition manuelle requise')
        if sum(money(a['amount']) for a in allocations) != amount:
            raise ValueError('Paiement groupe different de la somme des restes a payer')
        result['reason'] = 'Paiement partiel' if len(allocations) == 1 and money(allocations[0]['target']) < money(allocations[0]['total']) else 'Paiement groupe' if len(allocations) > 1 else 'Paiement integral'
        result['allocations'] = allocations
    except ValueError as exc:
        result['reason'] = str(exc)
    return result


def apply(pipe, record, tables):
    plan = json.loads(record['payload'])
    invalid = 'conflict' if record['status'] == 'applying' else 'stale'
    bank_matches = [(i + 2, row) for i, row in enumerate(tables[BANK]) if str(cell(row, 0)) == record['bank_id']]
    if len(bank_matches) != 1 or identity(bank_matches[0][1]) != plan['bank']:
        set_status(pipe, record['bank_id'], invalid)
        return
    bank_index, bank = bank_matches[0]
    if sum(identity(other)[1:] == identity(bank)[1:] for other in tables[BANK]) != 1:
        set_status(pipe, record['bank_id'], invalid)
        return
    expected_link = '; '.join(a['invoice'][1] for a in plan['allocations'])
    # A previous worker may have finished Sheets before the final DB write.
    if str(cell(bank, 12)).startswith('Rapproche') and (record['status'] != 'applying' or str(cell(bank, 11)) != expected_link):
        set_status(pipe, record['bank_id'], invalid)
        return
    targets = []
    for allocation in plan['allocations']:
        matches = [(i + 2, row) for i, row in enumerate(tables[allocation['tab']])
                   if str(cell(row, 0)) == allocation['invoice'][0]]
        if len(matches) != 1 or invoice_identity(matches[0][1]) != allocation['invoice']:
            set_status(pipe, record['bank_id'], invalid)
            return
        index, inv = matches[0]
        paid = money(cell(inv, 14) or '0')
        allowed = [money(allocation['before'])]
        if record['status'] == 'applying':
            allowed.append(money(allocation['target']))
        if paid not in allowed:
            set_status(pipe, record['bank_id'], invalid)
            return
        targets.append((allocation, index))
    day = str(cell(bank, 2))
    try:
        day = (date(1899, 12, 30) + timedelta(days=int(float(day)))).isoformat()
    except ValueError:
        date.fromisoformat(day)
    entries = []
    for allocation, index in targets:
        entry = ledger.build_entry(pipe._ledger_mapping(), company_id=pipe.company_id,
            kind='paiement_fournisseur' if allocation['tab'] == PURCHASES else 'reglement_client',
            piece='BANK-' + digest([pipe._sheet, record['bank_id'], allocation['invoice'][0]])[:24],
            entry_date=day, montant=money(allocation['amount']), reference=allocation['invoice'][1])
        if entry.statut != ledger.STATUT_VALIDEE:
            raise ValueError('Comptes banque/tiers incomplets : configurer avant application')
        entries.append(entry)
    set_status(pipe, record['bank_id'], 'applying')
    for allocation, index in targets:
        target = money(allocation['target'])
        status = 'Payee' if target == money(allocation['total']) else 'Partiellement payee'
        pipe._write(f"{allocation['tab']}!O{index}:P{index}", [[float(target), status]])
    for entry in entries:
        ledger.record(pipe._db, entry)
        # Recover a failed Sheets projection after the SQLite insert, without
        # appending the same payment twice on replay.
        rows = pipe._read(f'12_JOURNAL_COMPTABLE!A2:G{LIMIT + 1}')
        if len(rows) >= LIMIT:
            raise ValueError('Limite du journal atteinte')
        found = [r for r in rows if str(cell(r, 2)) == entry.piece]
        expected = ledger.sheet_rows(entry)
        if not found:
            pipe._append_journal(entry)
        elif len(found) != len(expected) or any(
            [str(cell(actual, i)) for i in range(5)] != wanted[:5]
            or any(money(cell(actual, i) or '0') != money(wanted[i] or '0') for i in (5, 6))
            for actual, wanted in zip(found, expected)
        ):
            raise ValueError('Projection du paiement incoherente : revue requise')
    pipe._write(f'{BANK}!L{bank_index}:M{bank_index}', [[expected_link, 'Rapproche - valide']])
    set_status(pipe, record['bank_id'], 'applied')


def refresh(pipe):
    """Re-read pending transactions; every application revalidates live inputs."""
    if not pipe.company_id:
        return  # legacy mono-tenant workbooks keep their existing behavior
    tables = read_tables(pipe)
    for record in records(pipe._db, pipe.company_id, pipe._sheet):
        if record['status'] in ('approved', 'applying'):
            try:
                apply(pipe, record, tables)
            except Exception as exc:
                safe_error = 'Application interrompue : consulter les journaux operateur.'
                if isinstance(exc, ValueError) and 'Comptes banque/tiers incomplets' in str(exc):
                    safe_error = 'Comptes banque/tiers incomplets : configurer avant application.'
                with closing(sqlite3.connect(pipe._db)) as c, c:
                    c.execute('UPDATE bank_reviews SET last_error=? WHERE company_id=? AND sheet_id=? AND bank_id=?',
                              (safe_error, pipe.company_id, pipe._sheet, record['bank_id']))
                raise
            tables = read_tables(pipe)  # later allocations see the new balance
    ids = [str(cell(row, 0)) for row in tables[BANK]]
    for row in tables[BANK]:
        key = str(cell(row, 0))
        if not key or str(cell(row, 12)).startswith('Rapproche'):
            continue
        tab = PURCHASES if cell(row, 6) not in ('', None) else SALES
        plan = proposal(row, tables[tab], tab)
        if ids.count(key) != 1:
            plan['reason'], plan['allocations'] = 'Identifiant bancaire repete : revue manuelle requise', []
        if any(other is not row and identity(other)[1:] == identity(row)[1:] for other in tables[BANK]):
            plan['reason'], plan['allocations'] = 'Operation repetee ou chevauchement de releves : verifier les justificatifs', []
        if cell(row, 11):
            plan['reason'], plan['allocations'] = 'Lien bancaire existant : revue manuelle requise', []
        save(pipe._db, pipe.company_id, pipe._sheet, key, plan)

