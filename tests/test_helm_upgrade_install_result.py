"""Coverage for `Helm.upgrade_install`'s applied/no-change classification.

Helm itself does not surface a machine-readable "no change" marker on
stdout. The lab converge path detects no-ops by comparing the release's
revision before and after the upgrade: if helm decided nothing actually
needed applying, the revision is held steady. The classification on the
returned `UpgradeResult` is what drives the rollout-wait skip downstream.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm, UpgradeResult
from chart_manager.plumbing.commands import CommandResult, CommandRunner


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._resolve_via_mise.cache_clear()


class ScriptedRunner(CommandRunner):
    """Runner whose stdout per call is driven by an ordered script.

    Each entry is `(predicate, stdout)`: the first predicate that matches
    the invocation's argv decides the response. Lets a single test set up
    distinct outputs for `helm list` vs `helm upgrade` without coupling
    to call order beyond what the scenario actually requires.
    """

    def __init__(self, *, list_responses: list[str], upgrade_response: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._list_responses = list(list_responses)
        self._upgrade_response = upgrade_response

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(args))
        argv = list(args)
        if "list" in argv:
            stdout = self._list_responses.pop(0) if self._list_responses else "[]"
            return CommandResult(args=tuple(args), returncode=0, stdout=stdout, stderr="")
        return CommandResult(
            args=tuple(args), returncode=0, stdout=self._upgrade_response, stderr=""
        )


def _release(revision: int) -> str:
    return json.dumps(
        [
            {
                "name": "demo",
                "namespace": "demo-ns",
                "revision": str(revision),
                "status": "deployed",
            }
        ]
    )


def test_upgrade_install_classifies_no_change_when_revision_steady(tmp_path: Path) -> None:
    # helm list returns revision=3 both before and after -> nothing rolled.
    runner = ScriptedRunner(list_responses=[_release(3), _release(3)])
    helm = Helm(runner=runner)

    result = helm.upgrade_install(
        "demo",
        tmp_path / "demo",
        namespace="demo-ns",
        timeout="1m",
        wait=False,
    )

    assert isinstance(result, UpgradeResult)
    assert result.status == "no-change"
    assert result.revision_before == 3
    assert result.revision_after == 3


def test_upgrade_install_classifies_applied_on_first_install(tmp_path: Path) -> None:
    # Before: release not present (empty list). After: revision=1.
    runner = ScriptedRunner(list_responses=["[]", _release(1)])
    helm = Helm(runner=runner)

    result = helm.upgrade_install(
        "demo",
        tmp_path / "demo",
        namespace="demo-ns",
        wait=False,
    )

    assert result.status == "applied"
    assert result.revision_before is None
    assert result.revision_after == 1


def test_upgrade_install_classifies_applied_on_revision_bump(tmp_path: Path) -> None:
    runner = ScriptedRunner(list_responses=[_release(2), _release(3)])
    helm = Helm(runner=runner)

    result = helm.upgrade_install(
        "demo",
        tmp_path / "demo",
        namespace="demo-ns",
        wait=False,
    )

    assert result.status == "applied"
    assert result.revision_before == 2
    assert result.revision_after == 3
