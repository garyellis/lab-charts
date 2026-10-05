"""`upgrade-finalize` through `finalize.run`, with the HEAD baseline served by `git show`."""

from pathlib import Path
from typing import Any

import pytest

from chart_manager.commands.upgrade import (
    FinalizeRequest,
    FinalizeResult,
    UpgradeError,
    finalize,
)
from tests.conftest import FakeCommandRunner, workspace_for


def _write_chart(tmp_path: Path, *, version: str = "1.2.3", dependency: str = "2.4.0") -> Path:
    chart = tmp_path / "charts" / "demo"
    chart.mkdir(parents=True, exist_ok=True)
    (chart / "Chart.yaml").write_text(
        "---\n"
        "apiVersion: v2\n"
        "annotations:\n"
        '  example.com/long: "This deliberately long annotation stays on one line while only the wrapper version changes during finalization and review."\n'
        "name: demo\n"
        "# wrapper stays independent\n"
        f'version: "{version}"\n'
        "dependencies:\n"
        "  - name: upstream\n"
        f"    version: {dependency}\n",
        encoding="utf-8",
    )
    return chart


def _image(name: str, current: str, new: str, **extra: str) -> dict[str, str]:
    """One Renovate callback update for a container image, as `dataFileTemplate` writes it."""
    return {"depName": name, "currentValue": current, "newValue": new, "datasource": "docker", **extra}


def _finalize(tmp_path: Path, baseline: str, *updates: dict[str, Any]) -> FinalizeResult:
    """Finalize charts/demo with `baseline` as its Chart.yaml at HEAD."""
    runner = FakeCommandRunner(returncode=128, stderr="fatal: unexpected git call").respond(
        ("git", "show", "HEAD:charts/demo/Chart.yaml"), stdout=baseline
    )
    request = FinalizeRequest(
        repo_root=tmp_path,
        chart_path=tmp_path / "charts" / "demo",
        update_data={"updates": list(updates)},
    )
    return finalize.run(request, workspace=workspace_for(tmp_path), runner=runner)


def _changelog(chart: Path) -> str:
    return (chart / "changelog.md").read_text(encoding="utf-8")


def test_major_image_update_changes_only_the_quoted_wrapper_version(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")

    result = _finalize(tmp_path, baseline, _image("api", "2.9.0", "3.0.0"))

    assert (result.previous_version, result.version, result.changed) == ("1.2.3", "2.0.0", True)
    written = (chart / "Chart.yaml").read_text(encoding="utf-8")
    assert written == baseline.replace('version: "1.2.3"', 'version: "2.0.0"', 1)
    assert _changelog(chart) == "## 2.0.0\n\n- api: 2.9.0 -> 3.0.0\n\n"


def test_minor_update_bumps_patch_once_on_replay(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")
    update = _image("api", "2.9.0", "2.10.0")

    assert _finalize(tmp_path, baseline, update).version == "1.2.4"
    replay = _finalize(tmp_path, baseline, update)

    assert replay.version == "1.2.4"
    assert replay.changed is False
    assert _changelog(chart).count("## 1.2.4") == 1


def test_newer_update_on_open_branch_rewrites_the_same_section(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")
    (chart / "changelog.md").write_text("## 1.2.3\n\n- api: 2.8.0 -> 2.9.0\n\n", encoding="utf-8")

    assert _finalize(tmp_path, baseline, _image("api", "2.9.0", "2.10.0")).version == "1.2.4"
    # Renovate commits a newer value onto the still-open branch. The baseline
    # has not moved, so the heading stays 1.2.4 and only the body changes.
    second = _finalize(tmp_path, baseline, _image("api", "2.9.0", "2.11.0"))

    assert second.changed is True
    assert _changelog(chart) == (
        "## 1.2.4\n\n- api: 2.9.0 -> 2.11.0\n\n## 1.2.3\n\n- api: 2.8.0 -> 2.9.0\n\n"
    )


def test_package_file_is_captured_without_changing_deduplication(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")

    # Renovate emits one update per file, so a tag pinned in both values files
    # arrives twice. The two entries must still collapse to one changelog line.
    result = _finalize(
        tmp_path,
        baseline,
        _image("api", "2.9.0", "2.10.0", packageFile="charts/demo/values.yaml"),
        _image("api", "2.9.0", "2.10.0", packageFile="charts/demo/values-prod.yaml"),
    )

    assert [update.package_file for update in result.updates] == ["charts/demo/values.yaml"]
    assert _changelog(chart) == "## 1.2.4\n\n- api: 2.9.0 -> 2.10.0\n\n"


def test_without_update_metadata_the_chart_dependency_diff_decides_the_bump(
    tmp_path: Path,
) -> None:
    chart = _write_chart(tmp_path, dependency="3.0.0")
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8").replace("3.0.0", "2.4.0")

    result = _finalize(tmp_path, baseline)

    assert result.version == "2.0.0"
    assert _changelog(chart) == "## 2.0.0\n\n- upstream: 2.4.0 -> 3.0.0\n\n"


def test_no_qualifying_change_does_not_bump(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")
    update = {
        "depName": "python", "currentValue": "1.0.0", "newValue": "2.0.0",
        "manager": "pep621", "datasource": "pypi",
    }  # fmt: skip

    result = _finalize(tmp_path, baseline, update)

    assert (result.version, result.changed) == ("1.2.3", False)
    assert (chart / "Chart.yaml").read_text(encoding="utf-8") == baseline
    assert not (chart / "changelog.md").exists()


def test_refuses_divergent_wrapper_version(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path, version="9.9.9")
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8").replace("9.9.9", "1.2.3")

    with pytest.raises(UpgradeError, match="diverged"):
        _finalize(tmp_path, baseline, _image("api", "2.0.0", "2.1.0"))


@pytest.mark.parametrize("version", ["1.02.3", "1.2.03"])
def test_refuses_a_wrapper_version_with_leading_zeros(tmp_path: Path, version: str) -> None:
    chart = _write_chart(tmp_path, version=version)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")

    with pytest.raises(UpgradeError, match=r"strict x\.y\.z"):
        _finalize(tmp_path, baseline)


def test_rejects_incomplete_qualifying_update_metadata(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")

    with pytest.raises(UpgradeError, match="require dependency"):
        _finalize(tmp_path, baseline, _image("", "1.0.0", "2.0.0"))


def test_an_unreadable_baseline_is_an_upgrade_error(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    runner = FakeCommandRunner(returncode=128, stderr="fatal: invalid object name 'HEAD'")

    with pytest.raises(UpgradeError, match="cannot read baseline"):
        finalize.run(
            FinalizeRequest(repo_root=tmp_path, chart_path=chart, update_data={"updates": []}),
            workspace=workspace_for(tmp_path),
            runner=runner,
        )
    assert runner.calls == [("git", "show", "HEAD:charts/demo/Chart.yaml")]
