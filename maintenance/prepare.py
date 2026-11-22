"""Prepare a local, bound program-maintenance bundle; never update the program."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import uuid

sys.dont_write_bytecode = True
KIT = Path(__file__).absolute().parents[1]
CONTROL = {'INSTALLATION.json', 'runtime-use.lock', 'UPDATE_PENDING.json'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n', encoding='utf-8')


def root_path(path):
    root = Path(os.path.abspath(path))
    for p in reversed((root, *root.parents)):
        info = p.lstat()
        if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Redirected program or preparation path.')
    if not root.is_dir():
        raise ValueError('Program folder is absent.')
    return root


def member(root, name, *, missing=False):
    rel = PurePosixPath(name)
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or
            rel.is_absolute() or any(x in ('', '.', '..') for x in name.split('/'))):
        raise ValueError('Invalid declared program path.')
    if (any(x.lower() in ('.git', '.codex', '.agents', 'data', 'evidence', 'transition-requests') or
            x.lower() == '.env' or x.lower().startswith('.env.') for x in rel.parts) or
            rel.suffix.lower() in ('.sqlite3', '.sqlite', '.db', '.pem', '.key')):
        raise ValueError('Work data, credentials or repository internals are not program inputs.')
    path = root
    for part in rel.parts:
        path = path/part
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Redirected program member.')
    return path


def inventory(root):
    files, directories = {}, []
    def visit(folder):
        for p in sorted(folder.iterdir()):
            info = p.lstat()
            if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('Redirected program inventory.')
            name = p.relative_to(root).as_posix()
            member(root, name)
            if stat.S_ISDIR(info.st_mode):
                directories.append(name); visit(p)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                files[name] = p
            else:
                raise ValueError('Nonregular or hard-linked program member.')
    visit(root)
    return files, sorted(directories)


def kit_manifest(root):
    p = member(root, 'MANIFEST.json')
    value = json.loads(p.read_text(encoding='utf-8'))
    names = value['files']
    if len({x.casefold() for x in names}) != len(names) or set(names) & CONTROL or 'MANIFEST.json' in names:
        raise ValueError('Ambiguous or site-specific distribution manifest.')
    for name, digest in names.items():
        if not isinstance(digest, str) or len(digest) != 64 or sha(member(root, name)) != digest:
            raise ValueError('Program manifest mismatch: '+name)
    return value


def expected_dirs(names):
    return sorted({p.as_posix() for name in names for p in PurePosixPath(name).parents if str(p) != '.'})


def join_existing(root):
    # The runtime is part of this maintenance kit's fixed inventory. It joins
    # the specified existing installation for this preparation process only.
    spec = importlib.util.spec_from_file_location('_wm_maintenance_runtime', KIT/'_workmap_runtime.py')
    runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
    runtime.ensure_reader(root)


def prepare(program, output, candidate=None, coordinated=False):
    program = root_path(program)
    if (program/'UPDATE_PENDING.json').exists():
        raise ValueError('An interrupted update must be inspected with its existing bundle.')
    initial = candidate is None
    if initial and any((program/name).exists() for name in CONTROL):
        raise ValueError('Existing installation controls must not be initialized again.')
    if not initial and (program/'runtime-use.lock').exists():
        join_existing(program)
    old_manifest = kit_manifest(program)
    old_files, old_dirs = inventory(program)
    allowed = set(old_manifest['files']) | {'MANIFEST.json'}
    meta = None
    if not initial:
        meta = json.loads(member(program, 'INSTALLATION.json').read_text(encoding='utf-8'))
        if os.path.normcase(os.path.abspath(meta['installation_path'])) != os.path.normcase(str(program)):
            raise ValueError('Installation location differs.')
        allowed |= {'INSTALLATION.json'}
        if 'runtime-use.lock' in old_files:
            gate = json.loads(old_files['runtime-use.lock'].read_text(encoding='utf-8'))
            if gate != dict(protocol=1, installation_id=meta['installation_id']):
                raise ValueError('Gate identity differs.')
            allowed.add('runtime-use.lock')
        if 'LOCAL_USAGE.md' in old_files:
            allowed.add('LOCAL_USAGE.md')
    if set(old_files) != allowed or old_dirs != expected_dirs(allowed):
        raise ValueError('Unexpected program files/directories; preserve them and inspect.')
    source = program if initial else root_path(candidate)
    new_manifest = old_manifest if initial else kit_manifest(source)
    new_files, new_dirs = inventory(source)
    if not initial and (set(new_files) != set(new_manifest['files'])|{'MANIFEST.json'} or new_dirs != expected_dirs(new_files)):
        raise ValueError('Candidate contains undeclared or site-specific inputs.')
    bootstrap = json.loads(member(source, 'BOOTSTRAP.json').read_text(encoding='utf-8'))
    if bootstrap['protocol'] != 1 or bootstrap['control_files'] != ['BOOTSTRAP.json', 'runtime-use.lock']:
        raise ValueError('Unknown fixed-entry protocol.')
    for name, digest in bootstrap['files'].items():
        if new_manifest['files'].get(name) != digest:
            raise ValueError('Fixed-entry manifest differs.')
    if not initial and not coordinated:
        if 'runtime-use.lock' not in old_files or 'BOOTSTRAP.json' not in old_files:
            raise ValueError('First transition needs --coordinated and an external stopped-usage boundary.')
        if old_files['BOOTSTRAP.json'].read_bytes() != new_files['BOOTSTRAP.json'].read_bytes():
            raise ValueError('Fixed entry changes need --coordinated; ordinary updates keep them unchanged.')
    identity = str(uuid.uuid4()) if initial else meta['installation_id']
    after_sources = dict(new_files)
    if not initial and 'LOCAL_USAGE.md' in old_files:
        after_sources['LOCAL_USAGE.md'] = old_files['LOCAL_USAGE.md']
    names = set(after_sources)|{'INSTALLATION.json','runtime-use.lock'}
    after_meta = dict(meta or {})
    after_meta.update(product_name='Work Map', installation_id=identity, installation_path=str(program),
        source_manifest_sha256=sha(source/'MANIFEST.json'), installed_files=sorted(names),
        distribution_version=new_manifest.get('version', 'unspecified'))
    after_meta.setdefault('distribution', 'Work Map local shared installation')
    output = Path(os.path.abspath(output))
    if output.exists() or output == program or output.is_relative_to(program) or program.is_relative_to(output):
        raise ValueError('Bundle must be a new folder separate from the program.')
    root_path(output.parent)
    output.mkdir()
    dump(output/'after/INSTALLATION.json', after_meta)
    if not initial and 'runtime-use.lock' in old_files:
        after_sources['runtime-use.lock'] = old_files['runtime-use.lock']
    else:
        dump(output/'after/runtime-use.lock', dict(protocol=1, installation_id=identity))
        after_sources['runtime-use.lock'] = output/'after/runtime-use.lock'
    after_sources['INSTALLATION.json'] = output/'after/INSTALLATION.json'
    all_names = sorted(set(old_files)|set(after_sources))
    specs = {name:dict(before=sha(old_files[name]) if name in old_files else None,
                      after=sha(after_sources[name]) if name in after_sources else None) for name in all_names}
    changes = [name for name in all_names if specs[name]['before'] != specs[name]['after']]
    if initial and set(changes) != {'INSTALLATION.json','runtime-use.lock'}:
        raise ValueError('Initial shared settings may only add the two bound control files.')
    for name in changes:
        for side, sources in [('before',old_files),('after',after_sources)]:
            if name not in sources:continue
            dest = output/side/name;src = sources[name]
            dest.parent.mkdir(parents=True,exist_ok=True)
            if src != dest:shutil.copyfile(src,dest)
    # The unchanged gate is needed for full-scope verification but is never
    # copied/replaced by a normal update.
    config = dict(protocol=1, purpose='workmap-program-initial' if initial else
        'workmap-program-coordinated' if coordinated else 'workmap-program-update',
        target=str(program), installation_id=identity, changes=changes, files=specs,
        directories=dict(before=old_dirs,after=expected_dirs(after_sources)))
    dump(output/'TRANSITION.json', config)
    for name in ('apply.ps1','file-functions.ps1'):
        shutil.copyfile(KIT/'maintenance'/name, output/name)
    script = "param([ValidateSet('Verify','Apply','Restore')][string]$Mode='Verify',[Parameter(Mandatory)][string]$ExpectedBundleHash,[switch]$UsageStopped)\n"
    script += "$arguments=@('-NoProfile','-File',(Join-Path $PSScriptRoot 'apply.ps1'),'-Bundle',$PSScriptRoot,'-Target','"+str(program).replace("'","''")+"','-ExpectedBundleHash',$ExpectedBundleHash,'-Mode',$Mode)\n"
    script += "if($UsageStopped){$arguments+='-UsageStopped'}\n& pwsh @arguments\nexit $LASTEXITCODE\n"
    (output/'run.ps1').write_text(script,encoding='utf-8',newline='\n')
    dump(output/'BUNDLE_MANIFEST.json',dict(files={p.relative_to(output).as_posix():sha(p) for p in output.rglob('*') if p.is_file()}))
    return dict(status='prepared',bundle=str(output),bundle_sha256=sha(output/'BUNDLE_MANIFEST.json'),
        mode=config['purpose'],changes=len(changes),program_changed=False,
        external_usage_boundary_required=initial or coordinated)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['enable','update'])
    parser.add_argument('--program',required=True,type=Path)
    parser.add_argument('--candidate',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--coordinated',action='store_true')
    args=parser.parse_args()
    try:
        if (args.operation=='update') != (args.candidate is not None):
            raise ValueError('update requires --candidate; enable does not accept one.')
        value=prepare(args.program,args.output,args.candidate,args.coordinated)
        print(json.dumps(value,ensure_ascii=False));return 0
    except (OSError,ValueError,KeyError,RuntimeError) as exc:
        print(json.dumps(dict(status='rejected',program_changed=False,error=str(exc)),ensure_ascii=False),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
