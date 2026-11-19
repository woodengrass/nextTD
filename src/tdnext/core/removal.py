from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RemovalRequest:
    plugin_id: str
    affected: frozenset[str]


class RemovalGuard(Protocol):
    def __call__(self, request: RemovalRequest) -> AbstractContextManager[tuple[str, ...]]:
        """Hold session-start exclusion until exit; yield blockers, or () only when proven safe."""
        ...


@contextmanager
def unavailable_guard(request: RemovalRequest) -> Iterator[tuple[str, ...]]:
    yield (f"Cannot uninstall {request.plugin_id}: session safety integration is unavailable",)
