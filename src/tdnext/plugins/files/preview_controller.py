from collections.abc import Callable
from functools import partial
from pathlib import Path

from textual.worker import Worker, WorkerState

from .browser_widgets import PreviewPane, single_line
from .preview import FilePreview


class PreviewController(PreviewPane):
    def __init__(self, selected: Callable[[], Path | None], read_preview: Callable[[Path], FilePreview]) -> None:
        super().__init__()
        self.selected = selected
        self.read_preview = read_preview
        self._revision = 0
        self._worker: Worker[FilePreview] | None = None
        self._active: tuple[int, Path] | None = None
        self._pending: tuple[int, Path] | None = None
        self._target: Path | None = None

    def reset(self, message: str) -> None:
        self._revision += 1
        self._target = None
        self._pending = None
        self.show_message(message)

    def select(self, target: Path | None, empty_message: str) -> None:
        if target == self._target:
            if target is None:
                self.show_message(empty_message)
            return
        self._target = target
        self._revision += 1
        if target is None:
            self._pending = None
            self.show_message(empty_message)
            return
        self.show_message(f"Loading preview: {single_line(target.name)}")
        self._pending = (self._revision, target)
        self._start_preview()

    def _start_preview(self) -> None:
        if self._worker is not None or self._pending is None:
            return
        request, self._pending = self._pending, None
        self._active = request
        self._worker = self.run_worker(
            partial(self.read_preview, request[1]), name="files-preview", thread=True, exit_on_error=False,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not self._worker or event.state not in {
            WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED,
        }:
            return
        worker, request = self._worker, self._active
        self._worker = None
        self._active = None
        if worker is not None and request is not None and request[0] == self._revision and (
            request[1] == self.selected()
        ):
            if worker.result is not None:
                self.show_preview(worker.result)
            elif worker.error is not None:
                self.show_message(f"Cannot preview: {worker.error}")
        self._start_preview()
