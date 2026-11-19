import os as _os

if "TDNEXT_PLUGIN_CODE" in _os.environ:
    from .core.plugin_imports import bootstrap as _bootstrap_plugins

    _bootstrap_plugins()
