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

from chart_manager.integrations.helm import Helm, UpgradeResult
from tests.conftest import FakeCommandRunner, Reply


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


@pytest.mark.parametrize(
    ("list_responses", "status", "revision_before", "revision_after"),
    [
        pytest.param([_release(3), _release(3)], "no-change", 3, 3, id="revision-steady"),
        pytest.param(["[]", _release(1)], "applied", None, 1, id="first-install"),
        pytest.param([_release(2), _release(3)], "applied", 2, 3, id="revision-bump"),
    ],
)
def test_upgrade_install_classifies_by_the_revision_before_and_after(
    tmp_path: Path,
    list_responses: list[str],
    status: str,
    revision_before: int | None,
    revision_after: int,
) -> None:
    helm = Helm(runner=_scripted(list_responses=list_responses))

    result = helm.upgrade_install(
        "demo",
        tmp_path / "demo",
        namespace="demo-ns",
        timeout="1m",
        wait=False,
    )

    assert isinstance(result, UpgradeResult)
    assert result.status == status
    assert result.revision_before == revision_before
    assert result.revision_after == revision_after


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
