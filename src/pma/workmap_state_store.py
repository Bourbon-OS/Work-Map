"""Distinct physical format; reuses only SQLite transactions/CAS/bounded roots."""
from dataclasses import dataclass
from pma.workmap_sqlite_store import SQLiteStore
from pma.workmap_store import CommittedVersion, CorruptStore, StoreConflict, _encode, _decode, _sha256
from pma.workmap_state_contract import validate

APPLICATION_ID = 0x574D5354  # WMST, never an old 0.4.x Store


@dataclass(frozen=True)
class PreparedState:
    store: object
    location: object
    expected_token: str
    raw: bytes
    token: str

    def commit(self, *, fault_at=None):
        if self.location != self.store.path.resolve(): raise ValueError('prepared location changed')
        if fault_at not in (None,'after_object','after_cache_trim','after_head'): raise ValueError('unknown fault')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old=self.store._read_version(db)
            candidate=_decode(self.raw,'prepared state')
            validate(candidate)
            if _sha256(self.raw)!=self.token:raise CorruptStore('prepared content hash differs')
            if old.token != self.expected_token: raise StoreConflict('current root changed')
            if candidate['project_id'] != old.snapshot['project_id'] or candidate['map_id'] != old.snapshot['map_id'] or candidate['revision']==old.snapshot['revision']:
                raise ValueError('same Map and fresh revision required')
            db.execute('INSERT INTO snapshots VALUES (?,?)',(self.token,self.raw))
            if fault_at=='after_object': raise RuntimeError('injected before commit')
            updated=db.execute('UPDATE current SET token=? WHERE id=1 AND token=?',(self.token,self.expected_token))
            if updated.rowcount != 1: raise StoreConflict('current root changed')
            self.store._trim_comparison_cache(db,self.token,self.expected_token)
            if fault_at=='after_cache_trim': raise RuntimeError('injected before commit after cache trim')
            db.execute('COMMIT')
            if fault_at=='after_head': raise RuntimeError('injected lost response after commit')
        return CommittedVersion(self.token,candidate)


class StateStore(SQLiteStore):
    @staticmethod
    def _profile(db):
        if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise CorruptStore('not a history-free Work Map Store')
        return 'none'

    @staticmethod
    def _retention(db):
        StateStore._profile(db)
        return 'current_and_previous'

    def initialize(self, snapshot):
        validate(snapshot)
        raw=_encode(snapshot);token=_sha256(raw)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with self.path.open('xb'): pass
        except FileExistsError as exc:
            raise StoreConflict('target exists; never overwrite an uncertain Store') from exc
        with self.connection(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('PRAGMA application_id='+str(APPLICATION_ID));db.execute('PRAGMA user_version=1')
            db.execute('CREATE TABLE snapshots (token TEXT PRIMARY KEY NOT NULL, body BLOB NOT NULL)')
            db.execute('CREATE TABLE current (id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL)')
            db.execute('INSERT INTO snapshots VALUES (?,?)',(token,raw));db.execute('INSERT INTO current VALUES (1,?)',(token,))
            db.execute('COMMIT')
        return CommittedVersion(token,_decode(raw,'state'))

    def read(self, *, read_only=False):
        result=super().read(read_only=read_only);validate(result.snapshot);return result

    def commit_validated(self, expected_token, candidate, *, fault_at=None):
        return self.prepare_state(expected_token,candidate).commit(fault_at=fault_at)

    def prepare_state(self, expected_token, candidate):
        validate(candidate);raw=_encode(candidate)
        return PreparedState(self,self.path.resolve(),expected_token,raw,_sha256(raw))
