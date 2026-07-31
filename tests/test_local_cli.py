"""Public ``chart-manager local`` vocabulary and delegation."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.cli import main
from chart_manager.services.clusters.development import (
    DevelopmentClusterActionResult,
    DevelopmentClusterResult,
)
from chart_manager.services.local_resources import ResolvedStackTarget

from .conftest import cli


def _chart(root: Path, name: str = "alloy") -> Path:
    path = root / "charts" / name
    path.mkdir(parents=True)
    (path / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    return path


def _stack(root: Path, name: str = "platform") -> Path:
    path = root / ".chart-manager" / "stacks" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
apiVersion: local.cmg.io/v1alpha1
kind: LocalStack
metadata:
  name: {name}
spec:
  releases:
    - type: oci
      name: metrics-server
      chart: oci://registry.example.test/charts/metrics-server
      version: 1.2.3
      namespace: kube-system
      values: []
      timeout: 5m
""".lstrip(),
        encoding="utf-8",
    )
    return path


def test_local_help_centers_the_three_lifecycle_verbs() -> None:
    result = cli("local", "--help")

    assert result.exit_code == 0
    for command in ("up", "down", "reset"):
        assert command in result.stdout
    for removed in ("ensure", "sync", "delete", "expose", "test"):
        assert removed not in result.stdout


def test_sandbox_group_is_removed_without_an_alias() -> None:
    result = cli("sandbox", "--help")

    assert result.exit_code == 2
    assert "No such command" in result.output


@pytest.mark.parametrize("command", ["up", "reset"])
def test_local_commands_require_exactly_one_explicit_selector(command: str) -> None:
    result = cli("local", command)

    assert result.exit_code != 0
    assert "select exactly one of --chart or --stack" in str(result.exception)


@pytest.mark.parametrize("command", ["up", "reset"])
def test_local_commands_use_named_selectors_without_a_positional_target(command: str) -> None:
    result = cli("local", command, "--help")

    assert result.exit_code == 0
    assert "--chart" in result.output
    assert "--stack" in result.output
    assert "--target" not in result.output
    assert "--namespace" not in result.output
    assert "--cluster-name" not in result.output
    assert "TARGET" not in result.output


def test_local_down_has_no_target_selector() -> None:
    help_result = cli("local", "down", "--help")
    rejected = cli("local", "down", "--chart", "cert-manager")

    assert help_result.exit_code == 0
    assert "--chart" not in help_result.output
    assert "--stack" not in help_result.output
    assert rejected.exit_code == 2


def test_local_up_rejects_the_old_positional_chart_shape(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    result = cli("local", "up", str(chart), "--root", str(tmp_path))

    assert result.exit_code == 2
    assert "unexpected extra argument" in result.output.lower()


def test_chart_up_delegates_profile_and_skip_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chart = _chart(tmp_path)
    calls: list[tuple[object, str | None, str, bool]] = []

    class Service:
        def up_target(
            self,
            target: object,
            *,
            profile: str | None,
            cluster_name: str,
            skip_installed: bool,
        ) -> DevelopmentClusterResult:
            calls.append((target, profile, cluster_name, skip_installed))
            return DevelopmentClusterResult()

    class Container:
        def development_cluster_service(self, _root: Path, *, progress: object) -> Service:
            return Service()

    monkeypatch.setattr(main, "_container", Container)
    result = cli(
        "local",
        "up",
        "--chart",
        str(chart),
        "--profile",
        "telemetry",
        "--skip-installed",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    target, profile, cluster_name, skip_installed = calls[0]
    assert target.kind == "chart"  # type: ignore[attr-defined]
    assert profile == "telemetry"
    assert cluster_name == "chart-manager"
    assert skip_installed is True


def test_named_stack_up_loads_the_authored_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stack(tmp_path)
    calls: list[object] = []

    class Service:
        def up_target(self, target: object, **_kwargs: object) -> DevelopmentClusterResult:
            calls.append(target)
            return DevelopmentClusterResult()

    class Container:
        def development_cluster_service(self, _root: Path, *, progress: object) -> Service:
            return Service()

    monkeypatch.setattr(main, "_container", Container)
    result = cli("local", "up", "--stack", "platform", "--root", str(tmp_path))

    assert result.exit_code == 0, result.output
    target = calls[0]
    assert isinstance(target, ResolvedStackTarget)
    assert target.name == "platform"
    assert target.stack.spec.releases[0].type == "oci"


@pytest.mark.parametrize("command", ["up", "reset"])
def test_profile_is_rejected_for_a_stack(
    tmp_path: Path,
    command: str,
) -> None:
    _stack(tmp_path)
    result = cli(
        "local",
        command,
        "--stack",
        "platform",
        "--profile",
        "minimal",
        "--root",
        str(tmp_path),
    )

    assert result.exit_code != 0
    assert "--profile is only valid for a chart target" in str(result.exception)


def test_down_and_reset_address_the_same_cluster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stack(tmp_path)
    down_calls: list[str] = []
    reset_calls: list[str] = []

    class Service:
        def down(self, cluster_name: str) -> DevelopmentClusterActionResult:
            down_calls.append(cluster_name)
            return DevelopmentClusterActionResult(cluster_name, changed=True)

        def reset_target(
            self,
            _target: object,
            *,
            profile: str | None,
            cluster_name: str,
        ) -> DevelopmentClusterResult:
            assert profile is None
            reset_calls.append(cluster_name)
            return DevelopmentClusterResult()

    class Container:
        def development_cluster_service(self, _root: Path, *, progress: object) -> Service:
            return Service()

    monkeypatch.setattr(main, "_container", Container)
    down = cli("local", "down", "--root", str(tmp_path))
    reset = cli("local", "reset", "--stack", "platform", "--root", str(tmp_path))
    assert down.exit_code == reset.exit_code == 0
    assert down_calls == ["chart-manager"]
    assert reset_calls == ["chart-manager"]


def test_chart_name_and_directory_resolve_to_the_same_target(tmp_path: Path) -> None:
    chart = _chart(tmp_path, "cert-manager")

    by_name = main._resolve_chart_target(tmp_path, "cert-manager")
    by_path = main._resolve_chart_target(tmp_path, "./charts/cert-manager")

    assert by_name.path == by_path.path == chart.resolve()


@pytest.mark.parametrize(
    ("extra", "expected_namespace"),
    [
        ([], None),
        (["--namespace", "override"], "override"),
    ],
)
def test_charts_test_uses_chart_option_and_optional_namespace_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    expected_namespace: str | None,
) -> None:
    _chart(tmp_path)
    calls: list[tuple[object, Path]] = []

    class Service:
        def run(self, request: object) -> None:
            calls.append((request, selected_charts_dir))

    selected_charts_dir = Path()

    class Container:
        def ephemeral_test_cluster_service(
            self,
            _root: Path,
            *,
            progress: object,
            charts_dir: Path,
        ) -> Service:
            nonlocal selected_charts_dir
            selected_charts_dir = charts_dir
            return Service()

    monkeypatch.setattr(main, "_container", Container)
    result = cli(
        "charts",
        "test",
        "--chart",
        "alloy",
        "--root",
        str(tmp_path),
        *extra,
    )

    assert result.exit_code == 0, result.output
    request, charts_dir = calls[0]
    assert request.chart == "alloy"  # type: ignore[attr-defined]
    assert request.namespace == expected_namespace  # type: ignore[attr-defined]
    assert charts_dir == Path("charts")


def test_charts_test_rejects_a_positional_chart(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    result = cli("charts", "test", str(chart), "--root", str(tmp_path))

    assert result.exit_code == 2
