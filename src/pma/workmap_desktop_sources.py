"""Desktop input labels and Map-local document references."""
from pathlib import Path, PurePosixPath


FIELDS = {'名前': 'name', 'Intent（目的）': 'intent', 'Origin（生まれた理由）': 'origin'}
KINDS = {'誤記録の訂正': 'correction', '同じ目的の明確化': 'refinement',
         '未確認目的の確定': 'confirmation', '別の目的への変更（新Workが必要）': 'material_change'}


def _source_path(repo, ref):
    parts = PurePosixPath(ref.removeprefix('repo:')).parts
    root = Path(repo).resolve()
    if (not ref.startswith('repo:') or not parts or parts[0] != 'docs'
            or '#' in ref or any(p in ('.', '..') or ':' in p or '\\' in p for p in parts)):
        raise ValueError('根拠は、このMapの資料フォルダ内の文書を選んでください。')
    path = (root / '/'.join(parts)).resolve()
    if not path.is_relative_to(root / 'docs') or path.suffix.lower() not in ('.txt', '.md', '.json'):
        raise ValueError('根拠資料がこのMapの文書範囲外です。')
    return path


def source_ref(repo, filename):
    path = Path(filename).resolve(); root = Path(repo).resolve()
    if not path.is_relative_to(root):
        raise ValueError('別Mapの根拠資料は使えません。')
    ref = 'repo:' + path.relative_to(root).as_posix()
    _source_path(root, ref)
    return ref


