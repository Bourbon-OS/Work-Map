"""Select the format of the one explicitly opened SQLite Map."""
from contextlib import closing
import sqlite3

from pma.workmap_state_contract import StateDenied
from pma.workmap_state_read import read_trial_state
from pma.workmap_state_store import APPLICATION_ID
from pma.workmap_store_types import MissingStore


def require_current_state(path):
    if not path.is_file():
        raise MissingStore('Mapの保存先を確認してください。新規作成のときだけ init を使います。')
    snapshot = open_normal_state(path)
    if snapshot is not None:
        return snapshot
    raise StateDenied('新形式のWork Mapを確認できません。保存先とバックアップを確認してください。')


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
