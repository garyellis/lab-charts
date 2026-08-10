"""Coverage for `Helm.upgrade_install`'s applied/no-change classification.

Helm itself does not surface a machine-readable "no change" marker on
stdout. The lab converge path detects no-ops by comparing the release's
revision before and after the upgrade: if helm decided nothing actually
needed applying, the revision is held steady. The classification on the
returned `UpgradeResult` is what drives the rollout-wait skip downstream.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm, UpgradeResult
from tests.conftest import FakeCommandRunner, Reply


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._clear_mise_cache()



def _scripted(*, list_responses: list[str], upgrade_response: str = "") -> FakeCommandRunner:
    """Answer `helm list` from an ordered script; anything else is the upgrade.

    Keyed on the subcommand rather than on call order so a scenario only
    couples to the sequence of *listings* it actually cares about. Listings
    past the script return `[]`, i.e. "no such release".
    """
    return FakeCommandRunner(stdout=upgrade_response).respond_each(
        lambda argv: "list" in argv,
        *(Reply(stdout=response) for response in list_responses),
        Reply(stdout="[]"),
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
    runner = _scripted(list_responses=[_release(3), _release(3)])
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
    runner = _scripted(list_responses=["[]", _release(1)])
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
    runner = _scripted(list_responses=[_release(2), _release(3)])
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


def test_upgrade_install_passes_an_exact_oci_version() -> None:
    runner = _scripted(list_responses=["[]", _release(1)])
    helm = Helm(runner=runner)

    helm.upgrade_install(
        "demo",
        "oci://example.test/charts/demo",
        namespace="demo-ns",
        version="1.2.3",
    )

    upgrade = next(argv for argv in runner.calls if "upgrade" in argv)
    assert upgrade[upgrade.index("--version") + 1] == "1.2.3"


def test_upgrade_install_uses_a_repository_url_without_managing_repo_state() -> None:
    runner = _scripted(list_responses=["[]", _release(1)])
    helm = Helm(runner=runner)

    helm.upgrade_install(
        "demo",
        "demo",
        namespace="demo-ns",
        version="1.2.3",
        repo="https://example.test/helm",
    )

    upgrade = next(argv for argv in runner.calls if "upgrade" in argv)
    assert upgrade[upgrade.index("--repo") + 1] == "https://example.test/helm"
    assert upgrade[upgrade.index("--version") + 1] == "1.2.3"
    assert not any("repo" in argv and "add" in argv for argv in runner.calls)
