"""Errors intended for the CLI or the desktop's status area."""


class PluginError(Exception):
    """An actionable plugin lifecycle failure."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class InstallationConsentRequired(PluginError):
    def __init__(self, actions: tuple[tuple[str, str], ...]) -> None:
        self.actions = actions
        super().__init__("External installation requires consent: " + "; ".join(text for _, text in actions))
