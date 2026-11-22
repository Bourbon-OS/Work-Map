"""Read one committed revision of an explicitly selected Work Map."""
import argparse
import json
from pathlib import Path
import sys

from pma.workmap_development_entry import read_new
from pma.workmap_normal_entry import require_current_state
from pma.workmap_tree import id_title_tree


def read_result(path, work=None, *, backend='sqlite', tree=False, brief=False, thread=False,
                evidence=False, expected_project_id=None, expected_revision=None,
                entry_id=None, pin_destination=None, actor=None, pin_inputs=False, evidence_repo=None):
    if not path.is_absolute() or backend != 'sqlite':
        return {'status': 'unknown', 'reason': 'MapのDBをPC内の絶対パスで指定してください。'}
    entries = [entry_id] if isinstance(entry_id, str) else entry_id or []
    return read_new(path, work, expected_project=expected_project_id, revision=expected_revision,
                    tree=tree or work is None, tree_reader=id_title_tree, brief=brief, thread=thread,
                    evidence=evidence, selected=entries, pin_destination=pin_destination, actor=actor,
                    pin_inputs=pin_inputs, evidence_repo=evidence_repo or path.parent.parent,
                    unsupported=False, state_reader=require_current_state)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--backend', choices=('sqlite',), default='sqlite')
    parser.add_argument('--work')
    parser.add_argument('--tree', action='store_true')
    views = parser.add_mutually_exclusive_group()
    for view in ('brief', 'thread', 'evidence'):
        views.add_argument('--' + view, action='store_true')
    views.add_argument('--pin-destination')
    parser.add_argument('--pin-inputs', action='store_true')
    parser.add_argument('--actor')
    parser.add_argument('--entry', action='append')
    parser.add_argument('--evidence-repo', type=Path)
    parser.add_argument('--expected-project')
    parser.add_argument('--expected-revision')
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args(argv)
    if not args.store.is_absolute():
        parser.error('--store has to be an absolute path')
    if (args.brief or args.evidence or args.thread or args.entry) and not args.work:
        parser.error('詳細の読取りには --work を指定してください。')
    if args.entry and not (args.brief or args.thread or args.evidence):
        parser.error('--entry は --brief、--thread、--evidence のいずれかと指定してください。')
    if args.tree and args.work and not args.brief:
        parser.error('--tree と --work を組み合わせる時は --brief を指定してください。')
    if args.pin_destination is not None:
        if not args.actor or not args.actor.strip() or not args.pin_destination.strip() or args.work or args.tree:
            parser.error('--pin-destination は --actor と指定します。')
    elif args.pin_inputs or args.actor is not None:
        parser.error('--pin-inputs と --actor は --pin-destination 用です。')
    result = read_result(args.store, args.work, backend=args.backend, tree=args.tree, brief=args.brief,
                         thread=args.thread, evidence=args.evidence, entry_id=args.entry,
                         expected_project_id=args.expected_project, expected_revision=args.expected_revision,
                         pin_destination=args.pin_destination, actor=args.actor, pin_inputs=args.pin_inputs,
                         evidence_repo=args.evidence_repo)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2,
                     separators=(',', ':') if args.compact else None))
    return 0 if result['status'] == 'confirmed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
