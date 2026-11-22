"""Optional desktop provider configured by the development checkout."""
from importlib import import_module

import pma


def extension_available():
    return hasattr(pma, 'WORKMAP_DESKTOP_EXTENSION')


def desktop_extension():
    if not extension_available():
        raise ValueError('この保存先のWork Mapを確認できません。')
    return import_module(pma.WORKMAP_DESKTOP_EXTENSION)
