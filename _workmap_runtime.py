"""Stable, standard-library-only Windows admission for one shared installation.

Prototype only. Readers retain one OS handle until process termination; they
never write a usage log or acquire the gate again for each Work operation.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import stat
import sys
from _thread import allocate_lock

GATE = 'runtime-use.lock'
PENDING = 'UPDATE_PENDING.json'
_readers = {}
_reader_lock = allocate_lock()
CLI_MODULES = ('check', 'pma.workmap_viewer', 'tools.add_workmap_work', 'tools.close_workmap_work', 'tools.move_workmap_pin', 'tools.read_workmap_store', 'tools.record_workmap_evidence', 'workmap')

class GateError(RuntimeError):
    status = 'shared_program_unavailable'
class GateBusy(GateError):
    status = 'shared_program_updating'
class RecoveryRequired(GateError):
    status = 'shared_program_recovery_required'

def _entry(path, *, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:return None
        raise GateError('Required shared-program file is missing.') from None
    if getattr(info,'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise GateError('Redirected shared-program path.')
    return info

def _root(path):
    path=Path(os.path.abspath(path))
    for item in reversed((path,*path.parents)):
        _entry(item)
    return path

def _api():
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
        ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE];api.CloseHandle.restype=wintypes.BOOL
    api.ReadFile.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
    api.ReadFile.restype=wintypes.BOOL
    return api

def _open(root, *, exclusive=False):
    info=_entry(root/GATE)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>4096:
        raise GateError('Invalid shared-program gate file.')
    api=_api()
    handle=api.CreateFileW(str(root/GATE),0x80000000,0 if exclusive else 1,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:
        error=ctypes.get_last_error()
        if error in (32,33):raise GateBusy('Shared program is in use or being maintained.')
        raise GateError('Cannot acquire the shared-program gate (Windows error %s).'%error)
    try:
        data=ctypes.create_string_buffer(4097);count=wintypes.DWORD()
        if not api.ReadFile(handle,data,4097,ctypes.byref(count),None):
            raise GateError('Cannot read shared-program gate identity.')
        if count.value>4096:raise GateError('Oversized shared-program gate identity.')
        value=json.loads(data.raw[:count.value].decode('utf-8'))
        if set(value)!={'protocol','installation_id'} or value['protocol']!=1:
            raise GateError('Unknown shared-program gate protocol.')
        meta_path=root/'INSTALLATION.json';meta_info=_entry(meta_path)
        if not stat.S_ISREG(meta_info.st_mode) or meta_info.st_size>131072:
            raise GateError('Invalid installation metadata.')
        meta=json.loads(meta_path.read_text(encoding='utf-8'))
        if (meta.get('installation_id')!=value['installation_id'] or
            os.path.normcase(os.path.abspath(meta.get('installation_path','')))!=os.path.normcase(str(root))):
            raise GateError('Shared-program installation identity differs.')
        if not exclusive and _entry(root/PENDING,missing=True) is not None:
            raise RecoveryRequired('A shared-program update needs recovery before use.')
        return handle
    except BaseException:
        api.CloseHandle(handle)
        raise

def ensure_reader(root):
    """Join once before loading program code in a shared installation."""
    root=Path(os.path.abspath(root));key=os.path.normcase(str(root))
    with _reader_lock:
        if key in _readers:return _readers[key]
        # lstat sees dangling/reparse control entries too. Only installed
        # layouts need the strict shared-root/ancestor checks.
        present=[_entry(root/name,missing=True) is not None for name in (GATE,PENDING,'INSTALLATION.json')]
        if not any(present):return None
        if os.name!='nt':raise GateError('Shared gate prototype requires Windows.')
        root=_root(root)
        handle=_open(root)
        _readers[key]=handle
    # Deliberately no atexit callback: other shutdown callbacks/threads can
    # still use code. Windows releases this noninheritable handle at exit.
    return handle

def _module_launch():
    """Identify the interpreter's requested -m, not an importing API host.

    The package is still being located only while argv[0] is '-m'. orig_argv
    identifies which module the interpreter was actually asked to launch.
    Stop parsing at -c, a script, or -- so user arguments cannot impersonate it.
    """
    if not sys.argv or sys.argv[0]!='-m':return None
    args=list(getattr(sys,'orig_argv',()))[1:];i=0
    while i<len(args):
        arg=args[i]
        if arg=='-m':return args[i+1] if i+1<len(args) else None
        if arg.startswith('-m') and len(arg)>2:return arg[2:]
        if arg in ('-c','--','-') or not arg.startswith('-'):return None
        if arg in ('-W','-X','--check-hash-based-pycs'):i+=1
        i+=1
    return None

def _is_cli(namespace):
    return namespace.get('__name__')=='__main__' or _module_launch() in CLI_MODULES

def _refuse(exc,namespace,entry):
    if not _is_cli(namespace):raise exc
    status=getattr(exc,'status','shared_program_unavailable')
    print(json.dumps({'status':status,'operation_started':False,'reason':str(exc)},ensure_ascii=False),file=sys.stderr)
    if str(entry).endswith('.pyw'):
        message=('共用版の更新が中断しています。保守担当が復旧してから開き直してください。'
            if isinstance(exc,RecoveryRequired) else
            '共用版を更新中のため起動できません。更新完了後に開き直してください。'
            if isinstance(exc,GateBusy) else
            '共用版を確認できないため起動できません。保守担当へ確認してください。')
        ctypes.windll.user32.MessageBoxW(None,message,'Work Map',0x40)
    raise SystemExit(75) from None

def join_package(root,namespace,entry):
    try:ensure_reader(root)
    except (GateError,OSError,ValueError) as exc:_refuse(exc,namespace,entry)

def run_payload(entry,namespace,root):
    """Preserve public module names and __file__, including direct scripts."""
    try:
        root=Path(os.path.abspath(root));ensure_reader(root)
        relative=Path(os.path.abspath(entry)).relative_to(root)
        source=root/'.workmap-payload'/relative
        _entry(source)
    except (GateError,OSError,ValueError) as exc:
        _refuse(exc,namespace,entry)
    # Compilation also occurs after admission. __file__ remains the public
    # entry for resources; traceback filenames point to the actual body.
    code=compile(source.read_bytes(),str(source),'exec',dont_inherit=True)
    exec(code,namespace)
