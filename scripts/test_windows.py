"""Run the unmodified suite on Windows, collecting SQLite cycles before unlink.

sqlite3.Connection context managers commit but do not close connections.
Linux permits unlinking their files; Windows does not. Collect unreachable
connections before fixture teardown without bypassing assertions or failures.
"""
import gc
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class CollectConnections:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        gc.collect()


if __name__ == '__main__':
    raise SystemExit(pytest.main(sys.argv[1:] or ['-q'], plugins=[CollectConnections()]))
