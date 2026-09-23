"""Local operator console. No Google writes and no accounting override.

This CLI is for administrators with filesystem access to the database.
The browser dashboard is read-only and can be restricted to one company.
Approval requires complete, explicit configuration and writes an audit event
in the same SQLite transaction as activation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal, InvalidOperation
import getpass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse

from app import companies as registry

SCHEMA = """
CREATE TABLE IF NOT EXISTS operation_audit (
    id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, doc_key TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operation_audit_company
    ON operation_audit(company_id, id);
"""


def ensure_schema(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(SCHEMA)


def read_connection(db_path: str):
    """Never creates a missing production DB or changes its schema."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def fingerprint(company: dict) -> str:
    # Operational timestamps do not invalidate a reviewed configuration.
    fields = {k: v for k, v in company.items()
              if k not in ('last_successful_cycle', 'created_at', 'activated_at')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def safe_link(value: str) -> str:
    try:
        url = urlparse(value)
        if (url.scheme == 'https' and url.hostname in ('drive.google.com', 'docs.google.com')
                and not url.username and not url.password and url.port in (None, 443)):
            return value
    except ValueError:
        pass
    return ''


def snapshot(db_path: str, company_id: str | None = None) -> dict:
    """Read a consistent, bounded overview; never extract/OCR/retry a document."""
    if company_id is not None:
        company_id = registry.normalize_company_id(company_id)
    with closing(read_connection(db_path)) as conn:
        conn.execute('BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'companies' not in tables:
            raise ValueError('registre multi-entreprises absent')
        companies = conn.execute('SELECT * FROM companies' +
                                 (' WHERE company_id=?' if company_id else '') + ' ORDER BY company_id',
                                 (company_id,) if company_id else ()).fetchall()
        if company_id and not companies:
            raise ValueError('entreprise inconnue')
        result = []
        for raw in companies:
            co = dict(raw)
            cid = co['company_id']
            counts, reviews, usage = {}, [], []
            if 'documents' in tables:
                cols = {r[1] for r in conn.execute('PRAGMA table_info(documents)')}
                if 'company_id' not in cols:
                    raise ValueError('documents non cloisonnes : migration requise')
                counts = dict(conn.execute('SELECT state, COUNT(*) FROM documents WHERE company_id=? GROUP BY state', (cid,)))
                for row in conn.execute(
                    "SELECT doc_key, filename, doc_type, numero, state, drive_link, payload, error, updated_at "
                    "FROM documents WHERE company_id=? AND state IN ('needs_review','failed','partial') "
                    "ORDER BY created_at, doc_key LIMIT 200", (cid,)
                ):
                    doc = dict(row)
                    try:
                        payload = json.loads(doc.pop('payload') or '{}')
                    except (ValueError, TypeError):
                        payload = {}
                    doc['reasons'] = payload.get('reasons', []) if isinstance(payload, dict) else []
                    doc['drive_link'] = safe_link(doc['drive_link'] or '')
                    # Raw exceptions can contain vendor credentials. Show an error
                    # indicator; detailed operator logs remain outside the portal.
                    doc['has_error'] = bool(doc.pop('error'))
                    reviews.append(doc)
            if 'llm_usage' in tables:
                usage = [dict(r) for r in conn.execute(
                    'SELECT level, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, '
                    'SUM(output_tokens) AS output_tokens FROM llm_usage WHERE company_id=? GROUP BY level,model', (cid,))]
            notes = [dict(r) for r in conn.execute(
                'SELECT doc_key, actor, action, detail, created_at FROM operation_audit '
                'WHERE company_id=? ORDER BY id DESC LIMIT 30', (cid,))] if 'operation_audit' in tables else []
            result.append({
                'company_id': cid, 'legal_name': co['legal_name'], 'status': co['status'],
                'approval_required': bool(co.get('approval_required', 0)),
                'config_validation_status': co['config_validation_status'],
                'sheet_link': safe_link('https://docs.google.com/spreadsheets/d/' + co['sheet_id']) if co['sheet_id'] else '',
                'drive_link': safe_link('https://drive.google.com/drive/folders/' + co['drive_folder_id']) if co['drive_folder_id'] else '',
                'last_successful_cycle': co['last_successful_cycle'],
                'revision': fingerprint(co), 'counts': counts, 'reviews': reviews,
                'usage': usage, 'audit': notes,
            })
        return {'generated_at': registry._now(), 'companies': result,
                'health': health_status(db_path), 'review_limit_per_company': 200}


def approve(db_path: str, company_id: str, config: dict, revision: str, actor: str) -> None:
    """Explicit local operator action; no bootstrap, no Google API, no defaults."""
    required = {'legal_name', 'ice', 'country', 'currency', 'allowed_vat_rates',
                'telegram_chat_id', 'account_mapping'}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError('configuration attendue : ' + ', '.join(sorted(required)))
    if not actor.strip() or not revision:
        raise ValueError('acteur et revision requis')
    if not isinstance(config['legal_name'], str) or len(config['legal_name'].strip()) < 3:
        raise ValueError('raison sociale requise')
    if config['country'] != 'MA' or config['currency'] != 'MAD':
        raise ValueError('activation controlee limitee au perimetre MA / MAD')
    if not isinstance(config['ice'], str) or not re.fullmatch(r'\d{15}', config['ice']):
        raise ValueError('ICE declare de 15 chiffres requis (aucune verification externe implicite)')
    if not re.fullmatch(r'-?[1-9]\d*', str(config['telegram_chat_id'])):
        raise ValueError('destination Telegram invalide')
    rates = config['allowed_vat_rates']
    if not isinstance(rates, list) or not rates:
        raise ValueError('taux de TVA explicites requis')
    try:
        rates = [Decimal(str(v)) for v in rates]
        if any(not r.is_finite() or not 0 <= r <= 100 for r in rates):
            raise ValueError('taux de TVA invalide')
    except InvalidOperation as exc:
        raise ValueError('taux de TVA invalide') from exc
    from app.ledger import TEMPLATE_ACCOUNTS
    mapping = config['account_mapping']
    if not isinstance(mapping, dict) or not set(TEMPLATE_ACCOUNTS) <= set(mapping):
        raise ValueError('mapping explicite des six comptes du journal requis')
    for role, value in mapping.items():
        code = value if isinstance(value, str) else (value[0] if isinstance(value, list) and len(value) == 2 else '')
        if not isinstance(code, str) or not re.fullmatch(r'\d{3,12}', code):
            raise ValueError('compte invalide : ' + str(role))
    ensure_schema(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM companies WHERE company_id=?', (company_id,)).fetchone()
        if row is None or fingerprint(dict(row)) != revision:
            raise ValueError('configuration modifiee : relire avant de valider')
        co = dict(row)
        if co['status'] != registry.PENDING_CONFIGURATION or not co.get('approval_required'):
            raise ValueError('seule une entreprise en attente de validation peut etre activee')
        if (co['config_validation_status'] != registry.CONFIG_OK or not co.get('bootstrap_initialized')
                or co.get('bootstrap_sheet_id') != co['sheet_id'] or not co['drive_folder_id']):
            raise ValueError('bootstrap incomplet : aucune activation')
        collision = conn.execute(
            'SELECT 1 FROM companies WHERE company_id<>? AND (sheet_id=? OR drive_folder_id=?)',
            (company_id, co['sheet_id'], co['drive_folder_id']),
        ).fetchone()
        if collision:
            raise ValueError('destinations partagees entre entreprises')
        conn.execute(
            'UPDATE companies SET legal_name=?,ice=?,country=?,currency=?,allowed_vat_rates=?, '
            'telegram_chat_id=?,account_mapping=?,approval_required=0,status=?,activated_at=? WHERE company_id=?',
            (config['legal_name'].strip(), config['ice'], 'MA', 'MAD', json.dumps([str(r) for r in rates]),
             str(config['telegram_chat_id']), json.dumps(mapping), registry.ACTIVE, registry._now(), company_id),
        )
        conn.execute('INSERT INTO operation_audit(company_id,actor,action,detail,created_at) VALUES (?,?,?,?,?)',
                     (company_id, actor, 'company_approved', json.dumps(config, ensure_ascii=False), registry._now()))


def add_note(db_path: str, company_id: str, doc_key: str, note: str, actor: str) -> None:
    if not 1 <= len(note.strip()) <= 2000 or not actor.strip():
        raise ValueError('note de 1 a 2000 caracteres et acteur requis')
    ensure_schema(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT state FROM documents WHERE company_id=? AND doc_key=?', (company_id, doc_key)).fetchone()
        if not row or row[0] not in ('needs_review', 'failed', 'partial'):
            raise ValueError('document absent de la file de revue de cette entreprise')
        conn.execute('INSERT INTO operation_audit(company_id,doc_key,actor,action,detail,created_at) VALUES (?,?,?,?,?,?)',
                     (company_id, doc_key, actor, 'review_note', note.strip(), registry._now()))


def health_status(db_path: str) -> dict:
    from app.cycle_health import read_status
    return read_status(db_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('snapshot', 'export', 'serve'):
        sub = commands.add_parser(name)
        scope = sub.add_mutually_exclusive_group(required=True)
        scope.add_argument('--company')
        scope.add_argument('--all', action='store_true', help='vue administrateur de toutes les entreprises')
        if name == 'export':
            sub.add_argument('--output', required=True)
        if name == 'serve':
            sub.add_argument('--port', type=int, default=8765)
    approve_cmd = commands.add_parser('approve')
    approve_cmd.add_argument('--company', required=True)
    approve_cmd.add_argument('--config', required=True)
    approve_cmd.add_argument('--revision', required=True)
    note_cmd = commands.add_parser('note')
    note_cmd.add_argument('--company', required=True)
    note_cmd.add_argument('--document', required=True)
    note_cmd.add_argument('--text', required=True)
    args = parser.parse_args()
    # Refuse typos before any write command can create an empty DB.
    if not Path(args.db).is_file():
        parser.error('base existante requise')
    try:
        if args.command == 'approve':
            approve(args.db, args.company, json.loads(Path(args.config).read_text(encoding='utf-8')),
                    args.revision, getpass.getuser())
            print('Entreprise validee. Aucun document rejoue par cette commande.')
        elif args.command == 'note':
            add_note(args.db, args.company, args.document, args.text, getpass.getuser())
            print('Note enregistree. Etat comptable inchange.')
        elif args.command == 'serve':
            from app.operations_web import serve
            serve(args.db, args.company, args.port)
        elif args.command == 'export':
            from app.operations_web import render
            Path(args.output).write_text(render(snapshot(args.db, args.company)), encoding='utf-8')
            print(args.output)
        else:
            print(json.dumps(snapshot(args.db, args.company), ensure_ascii=False, indent=2))
    except (ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
