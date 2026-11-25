import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    # The archive golden suite only runs under scripts/verify-dist.py, which
    # sets TDNEXT_VERIFIED_DIST_ROOT to the extracted distribution. Deselect
    # (not skip: the CI gate fails on skips) keeps normal developer pytest
    # meaningful without rerunning the archive through recursion.
    if config.option.collectonly and os.environ.get('TDNEXT_COLLECTION_REPORT'):
        return None
    if os.environ.get('TDNEXT_VERIFIED_DIST_ROOT') and os.environ.get('TDNEXT_GOLDEN_VIA_VERIFY_DIST') != '1':
        return None
    dropped = [item for item in items if item.path.name == 'test_desktop_golden.py']
    if dropped:
        items[:] = [item for item in items if item not in dropped]
        config.hook.pytest_deselected(items=dropped)
    return None


def pytest_deselected(items: list[pytest.Item]) -> None:
    report = os.environ.get('TDNEXT_DESELECTED_REPORT')
    if report:
        with Path(report).open('a', encoding='utf-8') as output:
            for item in items:
                print(item.nodeid, file=output)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_finish(session: pytest.Session) -> None:
    path = os.environ.get('TDNEXT_COLLECTION_REPORT')
    if session.config.option.collectonly and path:
        Path(path).write_text(''.join(f'{item.nodeid}\n' for item in session.items), encoding='utf-8')


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    path = os.environ.get('TDNEXT_PASSED_REPORT')
    if path and report.when == 'call' and report.passed:
        with Path(path).open('a', encoding='utf-8') as output:
            print(report.nodeid, file=output)
