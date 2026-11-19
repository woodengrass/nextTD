"""Claim the Linux process name so session processes match by name."""

import ctypes
import os

from .errors import PluginError

_PROCESS_NAME = b"tdnext"


def set_process_name(name: bytes = _PROCESS_NAME) -> bool:
    if ctypes.CDLL(None, use_errno=True).prctl(15, ctypes.c_char_p(name), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise PluginError(f"Cannot set process name: {os.strerror(error)}")
    return True
