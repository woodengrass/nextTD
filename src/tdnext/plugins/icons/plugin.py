"""Codicons 0.0.45 as mapped by Nerd Fonts 3.5.1; see README attribution."""

from typing import Final

from tdnext.api import IconMode

# Names are semantic, not Nerd Font names. Unknown keys always use safe ASCII.
_COMPATIBILITY: Final = {"files": "[F]", "monitor": "[M]", "shell": ">_", "settings": "[S]"}
_CODICONS: Final = {"files": "\uea83", "monitor": "\ueb31", "shell": "\uea85", "settings": "\ueb52"}


class CodiconsProvider:
    def resolve(self, key: str, mode: IconMode) -> str:
        match mode:
            case "compatibility":
                return _COMPATIBILITY.get(key, "[]")
            case "codicons":
                return _CODICONS.get(key, "[]")
