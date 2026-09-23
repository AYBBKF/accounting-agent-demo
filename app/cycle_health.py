"""Durable cycle telemetry, separate from process liveness. No raw exceptions."""
from __future__ import annotations

import json
from pathlib import Path
import time


def _path(db_path: str) -> Path:
    return Path(db_path).with_suffix('.cycles.json')


def read_status(db_path: str, *, now: float | None = None) -> dict:
    try:
        data = json.loads(_path(db_path).read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('invalid health')
        data['consecutive_failures'] = max(0, int(data.get('consecutive_failures', 0)))
        instant = time.time() if now is None else now
        if instant - float(data['updated_at']) > max(180, 3 * float(data['poll_seconds'])):
            data['status'] = 'stale'
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'unknown'}


class CycleHealth:
    def __init__(self, db_path: str, poll_seconds: int):
        self.path = _path(db_path)
        self.poll_seconds = poll_seconds
        previous = read_status(db_path)
        self.failures = int(previous.get('consecutive_failures', 0))
        self.last_success = previous.get('last_success')

    def finish(self, ok: bool, *, now: float | None = None) -> str:
        instant = time.time() if now is None else now
        was_failed = self.failures >= 3
        self.failures = 0 if ok else self.failures + 1
        if ok:
            self.last_success = instant
        data = {'status': 'ok' if ok else 'degraded', 'updated_at': instant,
                'last_success': self.last_success, 'consecutive_failures': self.failures,
                'poll_seconds': self.poll_seconds}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data), encoding='utf-8')
        temporary.replace(self.path)
        if self.failures == 3:
            return 'failure'
        if ok and was_failed:
            return 'recovered'
        return ''
