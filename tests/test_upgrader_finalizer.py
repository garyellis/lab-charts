from pathlib import Path

import pytest

from chart_manager.services.upgrader import (
    FinalizeRequest,
    UpdateMetadata,
    UpgradeError,
    UpgradeFinalizer,
    load_update_data,
)


class Baseline:
    def __init__(self, text: str) -> None:
        self.text = text

    def read(self, root: Path, revision: str, relative_path: Path) -> str:
        assert revision == "abc123"
        assert relative_path == Path("charts/demo/Chart.yaml")
        return self.text


def _write_chart(tmp_path: Path, *, version: str = "1.2.3", dependency: str = "2.4.0") -> Path:
    chart = tmp_path / "charts" / "demo"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\n"
        "name: demo\n"
        "# wrapper stays independent\n"
        f'version: "{version}"\n'
        "dependencies:\n"
        "  - name: upstream\n"
        f"    version: {dependency}\n",
        encoding="utf-8",
    )
    return chart


def _request(tmp_path: Path, chart: Path, updates: tuple[UpdateMetadata, ...]) -> FinalizeRequest:
    return FinalizeRequest(
        repo_root=tmp_path,
        chart_path=chart,
        updates=updates,
        baseline_ref="abc123",
    )


def test_major_image_update_bumps_wrapper_major_and_preserves_yaml(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")
    update = UpdateMetadata("api", "2.9.0", "3.0.0", datasource="docker")
    result = UpgradeFinalizer(Baseline(baseline)).finalize(_request(tmp_path, chart, (update,)))
    written = (chart / "Chart.yaml").read_text(encoding="utf-8")
    assert result.version == "2.0.0"
    assert result.bump == "major"
    assert '# wrapper stays independent\nversion: "2.0.0"' in written
    assert (chart / "changelog.md").read_text(encoding="utf-8") == (
        "## 2.0.0\n\n- api: 2.9.0 -> 3.0.0\n\n"
    )


def test_minor_update_bumps_patch_once_on_replay(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")
    update = UpdateMetadata("api", "2.9.0", "2.10.0", datasource="docker")
    finalizer = UpgradeFinalizer(Baseline(baseline))
    request = _request(tmp_path, chart, (update,))
    assert finalizer.finalize(request).version == "1.2.4"
    replay = finalizer.finalize(request)
    assert replay.version == "1.2.4"
    assert replay.changed is False
    assert (chart / "changelog.md").read_text(encoding="utf-8").count("## 1.2.4") == 1


def test_dependency_diff_is_reliable_fallback_and_major(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path, dependency="3.0.0")
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8").replace("3.0.0", "2.4.0")
    result = UpgradeFinalizer(Baseline(baseline)).finalize(_request(tmp_path, chart, ()))
    assert result.version == "2.0.0"
    assert result.updates[0].dependency == "upstream"


def test_no_qualifying_change_does_not_bump(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")
    update = UpdateMetadata("python", "1.0.0", "2.0.0", manager="pep621", datasource="pypi")
    result = UpgradeFinalizer(Baseline(baseline)).finalize(_request(tmp_path, chart, (update,)))
    assert result.bump is None
    assert not result.changed
    assert not (chart / "changelog.md").exists()


def test_refuses_divergent_wrapper_version(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path, version="9.9.9")
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8").replace("9.9.9", "1.2.3")
    update = UpdateMetadata("api", "2.0.0", "2.1.0", datasource="docker")
    with pytest.raises(UpgradeError, match="diverged"):
        UpgradeFinalizer(Baseline(baseline)).finalize(_request(tmp_path, chart, (update,)))


def test_loads_explicit_renovate_temp_data_outside_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    data = tmp_path / "renovate-data.json"
    data.write_text('{"updates": []}', encoding="utf-8")
    assert load_update_data(data, repo_root=repo) == {"updates": []}
    link = tmp_path / "link.json"
    link.symlink_to(data)
    with pytest.raises(UpgradeError, match="symlink"):
        load_update_data(link)


def test_rejects_oversized_renovate_data(tmp_path: Path) -> None:
    data = tmp_path / "renovate-data.json"
    data.write_text('{"padding": "xxxxxxxx"}', encoding="utf-8")
    with pytest.raises(UpgradeError, match="safety limit"):
        load_update_data(data, max_bytes=4)


def test_rejects_incomplete_qualifying_update_metadata(tmp_path: Path) -> None:
    chart = _write_chart(tmp_path)
    baseline = (chart / "Chart.yaml").read_text(encoding="utf-8")
    update = UpdateMetadata("", "1.0.0", "2.0.0", datasource="docker")
    with pytest.raises(UpgradeError, match="require dependency"):
        UpgradeFinalizer(Baseline(baseline)).finalize(_request(tmp_path, chart, (update,)))
