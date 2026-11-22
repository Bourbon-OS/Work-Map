"""Read and present one selected Work Map snapshot."""
import os
from pathlib import Path

from pma.workmap_state_contract import StateConflict, StateDenied, is_version_ref
from pma.workmap_state_sources import file_name, folder, local_file
from pma.workmap_state_store import StateStore


def trial_local_path(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
        raise StateDenied('試験用DBにはPC内の絶対パスを指定してください。')
    if os.name == 'nt':
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise StateDenied('ネットワークドライブは対象外です。')
    return path


def read_trial_state(path, *, project_id=None, map_id=None):
    path = trial_local_path(path)
    committed = StateStore(path).read(read_only=True)
    state = committed.snapshot
    if state['metadata']['store_role'] not in ('canonical_current', 'recovery_candidate'):
        raise StateDenied('現在Mapまたは復旧候補だけを閲覧できます。')
    if (project_id is not None and state['project_id'] != project_id
            or map_id is not None and state['map_id'] != map_id):
        raise StateConflict('保存先のMapが変わっています。開き直して確認してください。')
    return committed


def trial_source_locations(path, entries):
    """Point to carried files. Existence is separate from verifying their bytes."""
    refs = dict.fromkeys(ref for entry in entries for ref in entry.get('source_refs', []))
    root = folder(StateStore(path))
    locations = {}
    for ref in refs:
        carried = root / file_name(ref)
        locations[ref] = str(carried) if is_version_ref(ref) and local_file(carried).is_file() else None
    return locations
