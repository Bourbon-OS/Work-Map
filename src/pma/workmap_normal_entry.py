"""Select the format of the one explicitly opened SQLite Map."""
from contextlib import closing
import sqlite3

from pma.workmap_state_contract import StateDenied
from pma.workmap_state_read import read_trial_state
from pma.workmap_state_store import APPLICATION_ID


def is_state_store(path):
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        return db.execute('PRAGMA application_id').fetchone()[0] == APPLICATION_ID


def open_normal_state(path):
    if not is_state_store(path):
        return None
    snapshot = read_trial_state(path).snapshot
    if snapshot['metadata']['store_role'] != 'canonical_current':
        raise StateDenied('この保存先は切替前または書込み停止中です。確定した保存先を開いてください。')
    return snapshot
