"""Local rollback SQLite connection and snapshot row validation."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from pma.workmap_store_types import (
    CommittedVersion, CorruptStore, MissingStore, StoreError, _decode, _sha256,
)


class SQLiteConnection:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError('absolute path required')

    @contextmanager
    def connection(self, create=False, *, read_only=False):
        # rw prevents an unavailable/mistyped Store silently becoming empty.
        if create and read_only:
            raise ValueError('cannot create through a read-only connection')
        mode = 'ro' if read_only else ('rwc' if create else 'rw')
        if not create and not self.path.exists():
            raise MissingStore(str(self.path))
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + '?mode=' + mode,
                                 uri=True, timeout=2, isolation_level=None)
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise CorruptStore('SQLite Store requires rollback DELETE journal')
            if read_only:
                db.execute('PRAGMA query_only=ON')
            else:
                db.execute('PRAGMA synchronous=FULL')
            if not create:
                self._profile(db)
            yield db
        except sqlite3.Error as exc:
            raise StoreError(f'SQLite operation failed: {type(exc).__name__}: {exc}') from exc
        finally:
            if db is not None:
                db.close()  # rolls back any uncommitted transaction

    @staticmethod
    def version(row):
        if row is None:
            raise CorruptStore('missing current snapshot')
        token, raw = row
        if (not isinstance(token, str) or len(token) != 64
                or any(c not in '0123456789abcdef' for c in token)
                or not isinstance(raw, bytes)):
            raise CorruptStore('invalid snapshot row types')
        if _sha256(raw) != token:
            raise CorruptStore('snapshot hash differs')
        return CommittedVersion(token, _decode(raw, 'snapshot'))
