"""Public ``chart-manager local`` vocabulary and delegation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.cli import _container
from chart_manager.commands.local import run as local_run
from chart_manager.commands.local.models import (
    DevClusterActionResult,
    DevClusterPlan,
    DevClusterPlanEntry,
    DevClusterRelease,
    DevClusterResult,
    DevClusterStatus,
)
from chart_manager.commands.local.targets import ResolvedStackTarget
from chart_manager.plumbing.yaml_files import parse_yaml
from chart_manager.settings import Settings
from chart_manager.shared.charts.chart import resolve_chart_target
from tests.conftest import cli

pytestmark = pytest.mark.usefixtures("tmp_workspace")


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
apiVersion: chartmanager.io/v1alpha1
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
    tmp_path: Path, recorded: _RecordingService
) -> None:
    chart = _chart(tmp_path)

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
    target, options = recorded.requests[0]
    assert target.kind == "chart"
    assert options["profile"] == "telemetry"
    assert options["skip_installed"] is True


def test_named_stack_up_loads_the_authored_composition(
    tmp_path: Path, recorded: _RecordingService
) -> None:
    _stack(tmp_path)

    result = cli("local", "up", "--stack", "platform", "--root", str(tmp_path))

    assert result.exit_code == 0, result.output
    target, _options = recorded.requests[0]
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


# ----- the output vocabulary -------------------------------------------------
#
# Design doc 6.2: every command gets `json`, `auto` resolves from the
# environment, and a projection a command cannot produce is a usage error
# rather than a silently different answer. `local *` had no `-o` at all.


class _RecordingService:
    """Stands in for `commands.local`: records each call and returns empty results."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.requests: list[tuple[object, dict[str, object]]] = []

    def up(self, target: object, **options: object) -> DevClusterResult:
        self.calls.append("up")
        self.requests.append((target, options))
        return DevClusterResult()

    def reset(self, target: object, **options: object) -> DevClusterResult:
        self.calls.append("reset")
        self.requests.append((target, options))
        return DevClusterResult()

    def down(self, **_options: object) -> DevClusterActionResult:
        self.calls.append("down")
        return DevClusterActionResult("chart-manager", changed=True)

    def status(self, **_options: object) -> DevClusterStatus:
        self.calls.append("status")
        return DevClusterStatus(
            cluster_name="chart-manager",
            exists=True,
            context="kind-chart-manager",
            provider="kind",
            releases=(
                DevClusterRelease(
                    name="loki", namespace="observability", revision=2, status="deployed"
                ),
            ),
            urls=("https://loki.localhost/",),
        )

    def plan(
        self, target: object, *, profile: str | None, destroys: bool = False, **_options: object
    ) -> DevClusterPlan:
        self.calls.append("plan")
        return DevClusterPlan(
            command="reset" if destroys else "up",
            cluster_name="chart-manager",
            target=getattr(target, "name", None),
            target_kind=getattr(target, "kind", None),
            destroys=destroys,
            entries=(
                DevClusterPlanEntry(
                    chart="alloy", profile=profile or "minimal", namespace="obs", source="target"
                ),
            ),
        )

    def plan_down(self) -> DevClusterPlan:
        self.calls.append("plan_down")
        return DevClusterPlan(command="down", cluster_name="chart-manager")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _RecordingService:
    """Route every `local` command at one recording stand-in."""
    service = _RecordingService()
    for name in ("up", "reset", "down", "status", "plan", "plan_down"):
        monkeypatch.setattr(local_run, name, getattr(service, name))
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
    assert parse_yaml(result.stdout)["command"] == command


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


def test_ci_1_means_ci_to_auto_output_and_to_provision_hooks(
    tmp_path: Path, recorded: _RecordingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CI` is read once, in `Settings`, so every "am I in CI" agrees."""
    _chart(tmp_path)
    monkeypatch.setenv("CI", "1")
    # A terminal on stdout, so only CI can make `auto` pick json.
    monkeypatch.setenv("TTY_COMPATIBLE", "1")

    result = cli(*_local_argv("up", tmp_path))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["command"] == "up"
    _target, options = recorded.requests[0]
    assert options["run_hooks"] is False


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
    monkeypatch.setattr(
        local_run,
        "status",
        lambda **_options: DevClusterStatus(cluster_name="chart-manager", exists=False),
    )
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
        ("up", "plan", "up"),
        ("reset", "plan", "reset"),
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


def test_dry_run_reset_is_marked_destructive(tmp_path: Path, recorded: _RecordingService) -> None:
    """`up` and `reset` share a plan; only one of them deletes the cluster first."""
    _chart(tmp_path)

    up = cli(*_local_argv("up", tmp_path), "--dry-run", "-o", "json")
    reset = cli(*_local_argv("reset", tmp_path), "--dry-run", "-o", "json")

    assert json.loads(up.stdout)["destroys"] is False
    assert json.loads(reset.stdout)["destroys"] is True


def test_dry_run_renders_the_plan_as_a_table(tmp_path: Path, recorded: _RecordingService) -> None:
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

    workspace = _container.Container(Settings()).workspace(tmp_path)
    by_name = resolve_chart_target(workspace, "cert-manager")
    by_path = resolve_chart_target(workspace, "./charts/cert-manager")

    assert by_name.path == by_path.path == chart.resolve()
