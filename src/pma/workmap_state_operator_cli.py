"""Routine module commands for current Map data."""
import argparse
import json
from pathlib import Path

from pma.workmap_development_entry import write_new
from pma.workmap_normal_entry import require_current_state


def main(operation, argv=None, *, owner_confirmation=None):
    parser = argparse.ArgumentParser(description='Work Map: ' + operation)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--backend', choices=('sqlite',), default='sqlite')
    parser.add_argument('--actor')
    parser.add_argument('--evidence-repo', type=Path)
    parser.add_argument('--source', action='append', default=[])
    parser.set_defaults(resume=None)
    if operation == 'add':
        for field in ('parent', 'name', 'intent', 'origin'):
            parser.add_argument('--' + field)
        parser.set_defaults(root=False, display_id=None, owner_instruction=None,
                            reviewed_owner_add=False, reason=None)
    elif operation == 'pin':
        parser.add_argument('--inputs-json', required=True)
        parser.add_argument('--reviewed-pin-move', action='store_true')
        for field in ('action-rule-basis', 'dependency-basis', 'scope-basis'):
            parser.add_argument('--' + field)
        parser.set_defaults(owner_instruction=None, owner_source=None, reviewed_owner_pin=False)
    elif operation == 'close':
        for field in ('work', 'outcome', 'reason', 'owner-source'):
            parser.add_argument('--' + field)
        for field in ('evidence', 'residual'):
            parser.add_argument('--' + field, action='append', default=[])
        for flag in ('reviewed-close', 'release-own-pin', 'reviewed-owner-close'):
            parser.add_argument('--' + flag, action='store_true')
        parser.set_defaults(result=None, revisit=None, successor=[])
    elif operation == 'result':
        parser.add_argument('--work')
        parser.add_argument('--result', dest='summary')
        parser.add_argument('--why')
        parser.add_argument('--occurred-at')
        parser.set_defaults(related_entry=[])
    args = parser.parse_args(argv)
    if not args.store.is_absolute():
        parser.error('--store has to be an absolute path')
    try:
        packet = json.loads(args.inputs_json) if operation == 'pin' else None
        if operation == 'pin' and not isinstance(packet, dict):
            parser.error('--inputs-json requires the full input draft object')
        report = write_new(args, operation, packet=packet, confirmation=owner_confirmation,
                           state_reader=require_current_state)
    except ValueError as exc:
        report = {'result': {'result': 'rejected', 'reason': str(exc)}}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if (report['result'].get('result') == 'applied'
                 and report.get('readback', {}).get('status') == 'confirmed') else 2
