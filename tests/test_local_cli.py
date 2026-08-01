"""Public ``chart-manager local`` vocabulary and delegation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from chart_manager.cli import main
from chart_manager.services.clusters.development import (
    DevelopmentClusterActionResult,
    DevelopmentClusterPlan,
    DevelopmentClusterPlanEntry,
    DevelopmentClusterRelease,
    DevelopmentClusterResult,
    DevelopmentClusterStatus,
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


def test_local_help_centers_the_lifecycle_verbs_plus_status() -> None:
    result = cli("local", "--help")

    assert result.exit_code == 0
    for command in ("up", "down", "reset", "status"):
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


# ----- the output vocabulary -------------------------------------------------
#
# Design doc 6.2: every command gets `json`, `auto` resolves from the
# environment, and a projection a command cannot produce is a usage error
# rather than a silently different answer. `local *` had no `-o` at all.


class _RecordingService:
    """Records every call and returns empty results. Nothing here mutates."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def up_target(self, _target: object, **_kwargs: object) -> DevelopmentClusterResult:
        self.calls.append("up_target")
        return DevelopmentClusterResult()

    def reset_target(self, _target: object, **_kwargs: object) -> DevelopmentClusterResult:
        self.calls.append("reset_target")
        return DevelopmentClusterResult()

    def down(self, cluster_name: str) -> DevelopmentClusterActionResult:
        self.calls.append("down")
        return DevelopmentClusterActionResult(cluster_name, changed=True, port_forward_pid=7)

    def status(self, cluster_name: str) -> DevelopmentClusterStatus:
        self.calls.append("status")
        return DevelopmentClusterStatus(
            cluster_name=cluster_name,
            exists=True,
            context="kind-chart-manager",
            provider="kind",
            releases=(
                DevelopmentClusterRelease(
                    name="loki", namespace="observability", revision=2, status="deployed"
                ),
            ),
            urls=("https://loki.localhost/",),
        )

    def plan_target(
        self, target: object, *, profile: str | None, cluster_name: str, destroys: bool = False
    ) -> DevelopmentClusterPlan:
        self.calls.append("plan_target")
        return DevelopmentClusterPlan(
            command="reset" if destroys else "up",
            cluster_name=cluster_name,
            target=getattr(target, "name", None),
            target_kind=getattr(target, "kind", None),
            destroys=destroys,
            entries=(
                DevelopmentClusterPlanEntry(
                    chart="alloy", profile=profile or "minimal", namespace="obs", source="target"
                ),
            ),
        )

    def plan_down(self, cluster_name: str) -> DevelopmentClusterPlan:
        self.calls.append("plan_down")
        return DevelopmentClusterPlan(command="down", cluster_name=cluster_name)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _RecordingService:
    """Route every `local` command at one recording service."""
    service = _RecordingService()

    class Container:
        def development_cluster_service(self, _root: Path, *, progress: object) -> _RecordingService:
            return service

    monkeypatch.setattr(main, "_container", Container)
    return service


def _local_argv(command: str, root: Path) -> list[str]:
    """The minimum argv for one `local` verb; `up`/`reset` need a selector."""
    selector = ["--chart", "alloy"] if command in {"up", "reset"} else []
    return ["local", command, *selector, "--root", str(root)]


@pytest.mark.parametrize("command", ["up", "down", "reset", "status"])
def test_every_local_command_emits_a_json_document_on_stdout(
    tmp_path: Path, recorded: _RecordingService, command: str
) -> None:
    """One vocabulary, and the payload is the only thing on stdout."""
    _chart(tmp_path)

    result = cli(*_local_argv(command, tmp_path), "-o", "json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == command
    assert payload["cluster_name"] == "chart-manager"
    assert payload["ok"] is True


@pytest.mark.parametrize("command", ["up", "down", "reset", "status"])
def test_every_local_command_emits_yaml(
    tmp_path: Path, recorded: _RecordingService, command: str
) -> None:
    _chart(tmp_path)

    result = cli(*_local_argv(command, tmp_path), "-o", "yaml")

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.stdout)["command"] == command


@pytest.mark.parametrize("command", ["up", "down", "reset", "status"])
def test_auto_resolves_to_json_in_ci(
    tmp_path: Path,
    recorded: _RecordingService,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """`-o auto` is the default and must go through the shared `_auto` logic."""
    _chart(tmp_path)
    monkeypatch.setenv("CI", "true")

    result = cli(*_local_argv(command, tmp_path))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["command"] == command


@pytest.mark.parametrize("command", ["up", "down", "reset", "status"])
def test_the_global_output_flag_reaches_every_local_command(
    tmp_path: Path, recorded: _RecordingService, command: str
) -> None:
    _chart(tmp_path)

    result = cli("-o", "json", *_local_argv(command, tmp_path))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["command"] == command


@pytest.mark.parametrize("command", ["up", "down", "reset", "status"])
def test_markdown_is_rejected_rather_than_silently_rendered(
    tmp_path: Path, recorded: _RecordingService, command: str
) -> None:
    """`md` is offered only where a markdown projection exists (cli/output.py)."""
    _chart(tmp_path)

    result = cli(*_local_argv(command, tmp_path), "-o", "md")

    assert result.exit_code == 2
    assert "md" in result.output


def test_status_renders_a_human_table_on_stdout(
    tmp_path: Path, recorded: _RecordingService
) -> None:
    """The whole report is the projection, so none of it hides on stderr."""
    result = cli("local", "status", "--root", str(tmp_path), "-o", "table")

    assert result.exit_code == 0, result.output
    for token in ("chart-manager", "running", "kind-chart-manager", "loki", "deployed"):
        assert token in result.stdout
    assert "https://loki.localhost/" in result.stdout


def test_status_exits_zero_for_an_absent_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status` reports; it does not grade. An absent cluster is the answer."""

    class Service:
        def status(self, cluster_name: str) -> DevelopmentClusterStatus:
            return DevelopmentClusterStatus(cluster_name=cluster_name, exists=False)

    class Container:
        def development_cluster_service(self, _root: Path, *, progress: object) -> Service:
            return Service()

    monkeypatch.setattr(main, "_container", Container)
    result = cli("local", "status", "--root", str(tmp_path), "-o", "json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["exists"] is False
    assert payload["ok"] is False


# ----- --dry-run -------------------------------------------------------------
#
# Design doc 6.3: print the plan in --output form, exit 0, mutate nothing.
# Accepted-and-ignored is forbidden, which is why every case below asserts
# on the *absence* of the mutating call and not only on the exit code.


@pytest.mark.parametrize(
    ("command", "planner", "mutator"),
    [
        ("up", "plan_target", "up_target"),
        ("reset", "plan_target", "reset_target"),
        ("down", "plan_down", "down"),
    ],
)
def test_dry_run_plans_and_mutates_nothing(
    tmp_path: Path,
    recorded: _RecordingService,
    command: str,
    planner: str,
    mutator: str,
) -> None:
    _chart(tmp_path)

    result = cli(*_local_argv(command, tmp_path), "--dry-run", "-o", "json")

    assert result.exit_code == 0, result.output
    assert recorded.calls == [planner]
    assert mutator not in recorded.calls
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["command"] == command


def test_dry_run_reset_is_marked_destructive(
    tmp_path: Path, recorded: _RecordingService
) -> None:
    """`up` and `reset` share a plan; only one of them deletes the cluster first."""
    _chart(tmp_path)

    up = cli(*_local_argv("up", tmp_path), "--dry-run", "-o", "json")
    reset = cli(*_local_argv("reset", tmp_path), "--dry-run", "-o", "json")

    assert json.loads(up.stdout)["destroys"] is False
    assert json.loads(reset.stdout)["destroys"] is True


def test_dry_run_renders_the_plan_as_a_table(
    tmp_path: Path, recorded: _RecordingService
) -> None:
    _chart(tmp_path)

    result = cli(*_local_argv("up", tmp_path), "--dry-run", "-o", "table")

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.stdout
    for token in ("alloy", "minimal", "obs"):
        assert token in result.stdout
    # The reassurance is narration; a caller piping the plan wants the plan.
    assert "nothing was changed" in result.stderr


def test_dry_run_still_rejects_an_invalid_selection(tmp_path: Path) -> None:
    """A dry run is not a bypass: usage errors are decided before the plan."""
    result = cli("local", "up", "--dry-run", "--root", str(tmp_path))

    assert result.exit_code != 0
    assert "select exactly one of --chart or --stack" in str(result.exception)


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
        "chart",
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


def test_chart_test_accepts_the_chart_positionally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`chart test X` and `chart test --chart X` must reach the same request.

    P1.2 adds the positional and keeps `--chart` permanently, so the two
    spellings are not a migration -- they are two ways to say one thing, and
    a divergence between them would be invisible until CI (which uses the
    flag) and a human (who uses the argument) disagreed.
    """
    _chart(tmp_path)
    charts: list[str] = []

    class Service:
        def run(self, request: object) -> None:
            charts.append(request.chart)  # type: ignore[attr-defined]

    class Container:
        def ephemeral_test_cluster_service(
            self, _root: Path, *, progress: object, charts_dir: Path
        ) -> Service:
            return Service()

    monkeypatch.setattr(main, "_container", Container)
    positional = cli("chart", "test", "alloy", "--root", str(tmp_path))
    flag = cli("chart", "test", "--chart", "alloy", "--root", str(tmp_path))

    assert positional.exit_code == flag.exit_code == 0, positional.output
    assert charts == ["alloy", "alloy"]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="neither"),
        pytest.param(["alloy", "--chart", "alloy"], id="both"),
    ],
)
def test_chart_test_requires_exactly_one_chart(tmp_path: Path, argv: list[str]) -> None:
    """Naming the chart twice is as unusable as not naming it at all.

    Silently letting one spelling win would make `chart test a --chart b`
    install *something*, and which one is not guessable from the command line.
    """
    _chart(tmp_path)

    result = cli("chart", "test", *argv, "--root", str(tmp_path))

    assert result.exit_code != 0
    assert "exactly one chart" in str(result.exception)
