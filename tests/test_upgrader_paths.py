from pathlib import Path

import pytest

from chart_manager.services.upgrader import UpgradeError, resolve_chart_path


def _chart(root: Path, name: str = "demo") -> Path:
    path = root / "charts" / name
    path.mkdir(parents=True)
    (path / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 1.2.3\n", encoding="utf-8"
    )
    return path


def test_resolves_chart_name_relative_and_absolute(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    for requested in (Path("demo"), Path("charts/demo"), chart):
        root, resolved, metadata = resolve_chart_path(tmp_path, requested)
        assert root == tmp_path
        assert resolved == chart
        assert metadata["name"] == "demo"


def test_rejects_escape_and_chart_identity_mismatch(tmp_path: Path) -> None:
    outside = _chart(tmp_path.parent / f"{tmp_path.name}-outside")
    with pytest.raises(UpgradeError, match="inside repository"):
        resolve_chart_path(tmp_path, outside)
    chart = _chart(tmp_path, "wrong")
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: other\nversion: 1.2.3\n", encoding="utf-8"
    )
    with pytest.raises(UpgradeError, match="does not match"):
        resolve_chart_path(tmp_path, chart)


def test_rejects_symlinked_chart_or_chart_yaml(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    link = tmp_path / "charts" / "linked"
    link.symlink_to(chart, target_is_directory=True)
    with pytest.raises(UpgradeError, match="symlink"):
        resolve_chart_path(tmp_path, link)

