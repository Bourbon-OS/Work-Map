"""Connect both desktop forms to the Store selected by the viewer."""
from pma.workmap_desktop_extension import desktop_extension
from pma.workmap_state_entry import (
    inspect_trial_change, prepare_trial_add, prepare_trial_edit, save_trial_change,
)
from pma.workmap_state_read import read_trial_state


def prepare_add(source, *args, **kwargs):
    prepare = prepare_trial_add if source.state_trial else desktop_extension().prepare_add
    return prepare(source, *args, **kwargs)


def prepare_edit(source, *args, **kwargs):
    prepare = prepare_trial_edit if source.state_trial else desktop_extension().prepare_edit
    return prepare(source, *args, **kwargs)


def save_add(prepared):
    if prepared.request.get('format_version') == 'workmap-state-1':
        return save_trial_change(prepared)
    return desktop_extension().save_add(prepared)


def save_edit(prepared):
    if prepared.request.get('format_version') == 'workmap-state-1':
        return save_trial_change(prepared)
    return desktop_extension().save_edit(prepared)


def inspect_add_result(prepared):
    if prepared.request.get('format_version') == 'workmap-state-1':
        return inspect_trial_change(prepared)
    return desktop_extension().inspect_add_result(prepared)


def inspect_edit_result(prepared):
    if prepared.request.get('format_version') == 'workmap-state-1':
        return inspect_trial_change(prepared)
    return desktop_extension().inspect_edit_result(prepared)


def read_snapshot(source):
    if source.state_trial:
        return read_trial_state(source.path, project_id=source.project_id, map_id=source.map_id).snapshot
    return desktop_extension().read_snapshot(source)
