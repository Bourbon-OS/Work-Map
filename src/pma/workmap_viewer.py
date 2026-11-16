"""Stable Work Map admission facade."""
from pathlib import Path as _wm_Path
import importlib.util as _wm_util, os as _wm_os, sys as _wm_sys
_wm_root=_wm_Path(__file__).absolute().parents[2]
_wm_key='_work_map_runtime:'+_wm_os.path.normcase(_wm_os.path.abspath(_wm_root))
_wm_runtime=_wm_sys.modules.get(_wm_key)
if _wm_runtime is None:
    _wm_spec=_wm_util.spec_from_file_location(_wm_key,_wm_root/'_workmap_runtime.py')
    _wm_new=_wm_util.module_from_spec(_wm_spec)
    _wm_spec.loader.exec_module(_wm_new)
    _wm_runtime=_wm_sys.modules.setdefault(_wm_key,_wm_new)
if _wm_Path(_wm_runtime.__file__).absolute()!=_wm_root/'_workmap_runtime.py':
    raise ImportError('Work Map runtime identity differs.')
_wm_runtime.run_payload(__file__,globals(),_wm_root)
