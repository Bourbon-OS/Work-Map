"""Personal local trial: thin CLI over the existing Work Map Core (Python 3.11+)."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]

DB = ROOT / 'data' / 'workmap.sqlite3'
OWNER = 'actor:local-owner'


def map_location(map_dir=None):
    """Resolve one invocation; keep the program and Map locations separate."""
    if map_dir is None:
        return ROOT, DB
    directory = Path(map_dir)
    if not directory.is_absolute():
        raise ValueError('--map-dirにはMapフォルダの絶対パスを指定してください。')
    directory = directory.resolve()
    return directory, directory / 'data/workmap.sqlite3'


def gui(actor, *, map_dir=None):
    map_root, db = map_location(map_dir)
    import tkinter as tk
    from pma.workmap_viewer import WorkMapViewer
    from pma.workmap_normal_entry import require_current_state
    from pma.workmap_store_types import StoreError
    preferred = None
    problem = 'Mapの保存先と復旧手順を確認してください。'
    if db.exists():
        try:
            state = require_current_state(db)
            preferred = (state['project_id'], db.resolve())
        except (OSError, ValueError, StoreError, sqlite3.Error) as exc:
            problem = str(exc)
    root = tk.Tk()
    app = WorkMapViewer(root, actor_names={OWNER: 'Owner'}, operator_id=actor,
                       session_path=map_root / 'data/viewer-session.json', background_reads=True, task_index_path=None, shared_formats=True, state_only=True)
    if preferred is None:
        app.session_writable = False
        app.session_error = '以前の接続一覧は保全し、自動接続しません。' + problem
        app.status.configure(text=app.session_error)
    else:
        app.restore_session(preferred_source=preferred)
    if preferred is not None and (map_dir is not None or not any(s.path == db.resolve() for s in app.sources)):
        app.add_source(db, map_root)
    root.mainloop()


def state_trial_entry(args, map_root, db, owner_confirmation=None, opened=None):
    """Explicit isolated use through existing commands, with no fallback."""
    operations = {'show', 'thread', 'gui', 'resume', 'recovery-show'}
    if args.trial_write:
        operations.update(('add', 'backup', 'recover-check', 'permit', 'pin', 'reason', 'result', 'close'))
    if args.map_dir is None or args.command not in operations:
        if opened is not None:
            raise ValueError('新形式ではこの操作に対応していません。USAGE.mdの新形式の手順を確認してください。')
        raise ValueError('試験Mapには --map-dir と対応する操作を指定してください。書込みには --trial-write が必要です。')
    from pma.workmap_state_contract import read_work
    from pma.workmap_state_read import read_trial_state, trial_source_locations
    from pma.workmap_tree import id_title_tree

    if args.command in ('add', 'resume', 'backup', 'recover-check', 'recovery-show', 'permit', 'pin', 'reason', 'result', 'close'):
        result = state_trial_operation(args, map_root, db, owner_confirmation, opened)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status') in ('confirmed', 'saved_but_changed') else 2
    state = opened if opened is not None else read_trial_state(db).snapshot
    if args.command == 'gui':
        import tkinter as tk
        from pma.workmap_viewer import WorkMapViewer
        root = tk.Tk()
        app = WorkMapViewer(root, state_trial=True, trial_write=args.trial_write, operator_id=args.actor,
                           background_reads=True, actor_names={OWNER: 'Owner'})
        app.add_source(db, map_root)
        root.mainloop()
        return 0

    result = dict(status='confirmed', read_only=True, store_role=state['metadata']['store_role'],
                  project_id=state['project_id'], map_id=state['map_id'], revision=state['revision'])
    if args.command == 'show' and not args.work:
        role = '復旧候補' if result['store_role'] == 'recovery_candidate' else '新形式のMap'
        access = '・読取専用' if opened is None and not args.trial_write else ''
        print(f"{role}{access}  /  {state['revision']}")
        print(id_title_tree(state))
        return 0
    view = read_work(state, state['aliases'].get(args.work, args.work)) if args.work else None
    if args.command == 'show':
        if args.brief:
            for field in ('meaning', 'evidence'):
                if field in view:
                    view[field] = [{k: e[k] for k in ('id', 'summary')} for e in view[field]]
        result['work'] = view
    else:
        if view is not None and 'meaning' not in view:
            raise ValueError('このWorkの理由を現在の試験用DBから確認できません。')
        entries = view['meaning'] if view is not None else state['thread_entries']
        result['thread_entries'] = entries
        result['source_locations'] = trial_source_locations(db, entries)
        result['source_locations_status'] = 'presence_only'
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def state_trial_operation(args, map_root, db, owner_confirmation=None, opened=None):
    from pma.workmap_state_contract import is_version_ref, read_work
    from pma.workmap_state_entry import prepare_trial_add, read_trial_resume, resolve_trial_source, save_trial_change, trial_core
    from pma.workmap_state_read import read_trial_state, trial_local_path
    from pma.workmap_state_recovery import backup
    from pma.workmap_state_sources import local_file

    if args.command == 'resume':
        if not args.work:
            raise ValueError('新形式の再開では --work で現在の確認対象を指定してください。')
        if opened is None:
            return read_trial_resume(db, args.work)
        key = opened['aliases'].get(args.work, args.work)
        return dict(status='confirmed', past_result='unknown', observed_revision=opened['revision'],
                    store_role=opened['metadata']['store_role'], current=read_work(opened, key))
    if args.command == 'recovery-show':
        observed = read_trial_state(args.candidate)
        state = observed.snapshot
        if state['metadata']['store_role'] != 'recovery_candidate':
            raise ValueError('隔離した復旧候補を指定してください。')
        work = read_work(state, state['aliases'].get(args.work, args.work)) if args.work else None
        return dict(status='confirmed', read_only=True, store_role='recovery_candidate',
                    revision=state['revision'], project_id=state['project_id'], candidate_token=observed.token,
                    work=work, thread_entries=state['thread_entries'] if args.thread and work is None else
                    work.get('meaning') if args.thread else None)
    state = opened if opened is not None else read_trial_state(db).snapshot
    if state['metadata']['store_role'] != 'canonical_current':
        raise ValueError('この試験候補は読み取り専用です。')
    if args.command == 'backup':
        core = trial_core(db, map_root)
        data = json.dumps(backup(core.store, core.resolver), ensure_ascii=False).encode('utf-8')
        with local_file(trial_local_path(args.output)).open('xb') as stream:
            stream.write(data)
        return dict(status='confirmed', path=str(args.output), sha256=hashlib.sha256(data).hexdigest())

    from pma.workmap_state_trial_integrated import (
        COMMANDS, apply_command, capture_personal_instruction, personal_owner_grant, store_scope,
    )
    if args.command in COMMANDS:
        return apply_command(args, map_root, db, state, owner_confirmation, normal=opened is not None)

    from types import SimpleNamespace
    refs = args.source
    if args.reason or not args.owner_instruction.strip():
        raise ValueError('追加には実際のOwner指示を指定してください。別に残す判断理由は reason で記録できます。')
    seed = personal_owner_grant(state) if opened is not None and not refs else None
    if seed is not None:
        parent = state['aliases'].get(args.parent, args.parent)
        if parent not in state['map']['members']:
            raise ValueError('現在のMapの親Workを指定してください。')
        if seed['grantee'] != args.actor or seed['constraints'] != list(store_scope(state, db)):
            raise ValueError('この保存先でMapを初期設定した編集者を確認するか、根拠を --source で指定してください。')
        refs = [capture_personal_instruction(map_root, args.owner_instruction)]
    expected_versions = {}
    matches = False
    for ref in refs:
        raw = resolve_trial_source(map_root, ref)
        sha = hashlib.sha256(raw).hexdigest()
        if is_version_ref(ref) and not ref.endswith('#sha256=' + sha):
            raise ValueError('指定したOwner指示の版が変わっています。')
        expected_versions[ref.split('#sha256=', 1)[0]] = sha
        matches = matches or raw.decode('utf-8').strip() == args.owner_instruction.strip()
    if not matches:
        raise ValueError('同じ内容のOwner指示が記録された --source を指定してください。')
    parent = state['aliases'].get(args.parent, args.parent)
    source = SimpleNamespace(path=db, evidence_root=map_root, project_id=state['project_id'],
                             state_trial=True, map_id=state['map_id'])
    request = prepare_trial_add(source, state, parent=parent, name=args.name, intent=args.intent,
                                origin=args.origin, actor=args.actor, refs=list(expected_versions))
    current_versions = {binding['ref']: binding['sha256'] for binding in request.bindings}
    if any(current_versions.get(ref) != sha for ref, sha in expected_versions.items()):
        raise ValueError('確認したOwner指示の版が追加の準備中に変わりました。')
    return save_trial_change(request)


def recover(args, db):
    from pma.workmap_state_read import read_trial_state, trial_local_path
    from pma.workmap_state_recovery import restore_candidate, restore_reviewed
    from pma.workmap_state_sources import local_file

    backup_path = trial_local_path(args.backup.resolve())
    raw = local_file(backup_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        raise ValueError('バックアップの版が一致しません。')
    document = json.loads(raw)
    if args.command == 'restore':
        version = restore_reviewed(
            document, args.candidate, db, expected_token=args.candidate_token,
            project_id=args.project_id, owner_instruction=args.owner_instruction)
        return dict(status='confirmed', path=str(db), store_role='canonical_current',
                    revision=version.snapshot['revision'])

    target = trial_local_path(args.target.resolve())
    if target == db or not local_file(target.parent).is_dir():
        raise ValueError('復旧先は、作成済みの試験フォルダ内の新しいDBを指定してください。')
    candidate = restore_candidate(document, target)
    observed = read_trial_state(candidate.path)
    return dict(status='confirmed', path=str(candidate.path), store_role='recovery_candidate',
                revision=observed.snapshot['revision'], candidate_token=observed.token)



def main(argv=None, *, owner_confirmation=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='新形式のWork Map。--map-dirでMapのフォルダを指定。省略時は配布フォルダを使用。')
    parser.add_argument('--map-dir', type=Path, help='DBと根拠を置くMapフォルダの絶対パス')
    parser.add_argument('--state-trial', action='store_true', help='隔離した新形式の候補を読み取る')
    parser.add_argument('--trial-write', action='store_true', help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'add'):
        p = sub.add_parser(name)
        p.add_argument('--name', required=True)
        p.add_argument('--intent', required=True)
        p.add_argument('--origin', required=True)
        p.add_argument('--owner-instruction', required=True, help='本人が指示した登録内容。AIは本人の指示を捏造しない。')
        p.add_argument('--actor', default=OWNER)
        if name == 'add':
            p.add_argument('--parent', required=True)
            p.add_argument('--reason', help='Intent/Originだけで残せない特別な出生理由がある場合のみ')
            p.add_argument('--source', action='append', default=[], help='新形式で使う確認済みの資料')
    show = sub.add_parser('show'); show.add_argument('--work'); show.add_argument('--brief', action='store_true')
    thread = sub.add_parser('thread'); thread.add_argument('--work')
    resume = sub.add_parser('resume'); resume.add_argument('--work', required=True)
    backup = sub.add_parser('backup'); backup.add_argument('--output', required=True, type=Path)
    recovery_check = sub.add_parser('recover-check')
    recovery_check.add_argument('--backup', type=Path, required=True)
    recovery_check.add_argument('--sha256', required=True)
    recovery_check.add_argument('--target', type=Path, required=True, help='新しい隔離確認用DB。現用DBは指定しない。')
    view = sub.add_parser('recovery-show'); view.add_argument('--candidate', type=Path, required=True)
    view.add_argument('--work'); view.add_argument('--thread', action='store_true')
    restore = sub.add_parser('restore')
    for name in ('backup', 'candidate'):
        restore.add_argument('--' + name, type=Path, required=True)
    for name in ('sha256', 'candidate-token', 'project-id', 'owner-instruction'):
        restore.add_argument('--' + name, required=True)
    p = sub.add_parser('gui'); p.add_argument('--actor', default=OWNER)
    p = sub.add_parser('permit'); p.add_argument('--work', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--owner-instruction', required=True)
    for flag in ('edit', 'record', 'close', 'pin', 'map-add'): p.add_argument('--' + flag, action='store_true')
    p.add_argument('--source', help='新形式で使う範囲と版を確認済みのOwner指示')
    p = sub.add_parser('reason'); p.add_argument('--work', required=True); p.add_argument('--text', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--owner-instruction', required=True)
    p.add_argument('--source', action='append', default=[], help='新形式で指定する理由の根拠')
    p = sub.add_parser('result'); p.add_argument('--work', required=True); p.add_argument('--text', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--source', action='append', required=True)
    p = sub.add_parser('pin'); p.add_argument('--work', required=True); p.add_argument('--reason', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--owner-instruction', required=True)
    for field in ('action-rule-basis', 'dependency-basis', 'scope-basis'): p.add_argument('--' + field)
    p = sub.add_parser('close'); p.add_argument('--work', required=True)
    p.add_argument('--actor', default=OWNER); p.add_argument('--owner-instruction', required=True)
    p.add_argument('--outcome', choices=('completed', 'stopped', 'replaced'), required=True)
    p.add_argument('--reason', required=True); p.add_argument('--evidence', action='append', default=[])
    p.add_argument('--successor', action='append', default=[]); p.add_argument('--residual', action='append', default=[])
    p.add_argument('--release-own-pin', action='store_true')
    for command_parser in sub.choices.values():
        command_parser.add_argument('--map-dir', type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        map_dir = args.map_dir
        map_root, db = map_location(map_dir)
        if args.trial_write and not args.state_trial:
            raise ValueError('--trial-write には --state-trial が必要です。')
        if args.command in ('recover-check', 'restore'):
            if args.state_trial and (map_dir is None or not args.trial_write):
                raise ValueError('隔離試験には --map-dir と --trial-write が必要です。')
            result = recover(args, db)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.state_trial:
            return state_trial_entry(args, map_root, db, owner_confirmation)
        if args.command == 'gui':
            gui(args.actor, map_dir=map_dir); return 0
        if args.command == 'init':
            from pma.workmap_state_trial_integrated import initialize_personal_map
            result = initialize_personal_map(args, map_root, db)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        from pma.workmap_normal_entry import require_current_state
        if args.command == 'recovery-show':
            result = state_trial_operation(args, map_root, db)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        opened = require_current_state(db)
        args.trial_write = True
        args.map_dir = map_root
        return state_trial_entry(args, map_root, db, owner_confirmation, opened)
    except Exception as exc:
        print(json.dumps({'status': 'unknown_or_rejected', 'reason': str(exc),
            'recovery': '応答が分からないときは現在のMapを読み直してください。対象が分かる場合は resume --work <対象> で確認します。'},
            ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
