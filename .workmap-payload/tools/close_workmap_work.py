"""Operate on an explicitly selected Work Map."""
from pma.workmap_state_operator_cli import main as run_command


def main(argv=None, *, owner_confirmation=None):
    return run_command('close', argv, owner_confirmation=owner_confirmation)


if __name__ == '__main__':
    raise SystemExit(main())
