"""`chart test` and `chart teardown` at the CLI: flags, output modes and exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chart_manager.commands import test
from chart_manager.commands.test import cli as test_cli
from chart_manager.commands.test.models import (
    ActionKind,
    ActionOutcome,
    ActionTarget,
    LifecycleAction,
    LifecyclePlan,
)
from chart_manager.plumbing.errors import ChartManagerError, MissingToolError
from tests.conftest import MakeChart, cli

LOCAL_CLUSTER = (
    "apiVersion: chartmanager.io/v1alpha1\n"
    "kind: LocalCluster\n"
    "metadata: {name: default}\n"
    "spec:\n"
    "  cluster: {config: kind-config.yaml}\n"
    "  bootstrap: {releases: []}\n"
)


def _plan() -> LifecyclePlan:
    return LifecyclePlan(
        chart="alloy",
        profile="minimal",
        actions=tuple(
            LifecycleAction(
                action_id=f"chart-test.alloy.minimal.{kind.value}",
                kind=kind,
                target=ActionTarget("alloy", "minimal", release="alloy", namespace="observability"),
                chart_path=Path("charts/alloy"),
            )
            for kind in (ActionKind.INSTALL, ActionKind.HELM_TEST)
        ),
        warnings=("istio-base is owned by bootstrap",),
    )


class Calls:
    """What the stubbed `commands.test` entry points were called with."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.entry: list[str] = []
        self.workspaces: list[Any] = []
        self.outcome = test.ChartTestOutcome("alloy", "minimal", "chart-manager")
        self.teardown_outcome = test.TeardownOutcome("chart-manager", cluster_deleted=True)
        self.raises: Exception | None = None


@pytest.fixture
def calls(make_chart: MakeChart, root: Path, monkeypatch: pytest.MonkeyPatch) -> Calls:
    make_chart("alloy")
    recorded = Calls()

    def record(entry: str, result: Any):
        def stub(request: Any, *, workspace: Any, **_kw: Any) -> Any:
            recorded.entry.append(entry)
            recorded.requests.append(request)
            recorded.workspaces.append(workspace)
            if recorded.raises is not None:
                raise recorded.raises
            return result() if callable(result) else result

        return stub

    monkeypatch.setattr(test_cli, "plan", record("plan", _plan))
    monkeypatch.setattr(test_cli, "run", record("run", lambda: recorded.outcome))
    monkeypatch.setattr(test_cli, "teardown", record("teardown", lambda: recorded.teardown_outcome))
    return recorded


@pytest.mark.parametrize(
    ("extra", "namespace"), [([], None), (["--namespace", "override"], "override")]
)
def test_chart_test_passes_the_chart_its_charts_dir_and_the_namespace_override(
    chart_root: Path, calls: Calls, extra: list[str], namespace: str | None
) -> None:
    result = cli("chart", "test", "alloy", *extra)

    assert result.exit_code == 0, result.output
    assert calls.entry == ["run"]
    assert calls.requests[0].chart == "alloy"
    assert calls.requests[0].namespace == namespace
    assert calls.workspaces[0].spec.charts_dir == Path("charts")


def test_chart_test_passes_skip_requires_and_cluster_name(chart_root: Path, calls: Calls) -> None:
    result = cli("chart", "test", "alloy", "--skip-requires", "--cluster-name", "lab")

    assert result.exit_code == 0, result.output
    assert calls.requests[0].skip_requires is True
    assert calls.requests[0].cluster_name == "lab"


def test_a_failed_chart_test_raises_a_plain_error_naming_the_failed_action(
    chart_root: Path, calls: Calls
) -> None:
    calls.outcome = test.ChartTestOutcome(
        "alloy",
        "minimal",
        "chart-manager",
        actions=(
            ActionOutcome("chart-test.alloy.minimal.install", "install", "FAIL", "timed out"),
        ),
    )

    result = cli("chart", "test", "alloy")

    assert isinstance(result.exception, ChartManagerError)
    assert "chart-test.alloy.minimal.install" in str(result.exception)
    assert "timed out" in str(result.exception)


def test_a_missing_tool_reaches_main_unchanged(chart_root: Path, calls: Calls) -> None:
    """`main.py` maps MissingToolError to exit 127 (see test_exit_codes.py)."""
    calls.raises = MissingToolError("helm not found on PATH")

    result = cli("chart", "test", "alloy")

    assert isinstance(result.exception, MissingToolError)


def test_dry_run_prints_the_plan_and_runs_nothing(chart_root: Path, calls: Calls) -> None:
    result = cli("chart", "test", "alloy", "--dry-run")

    assert result.exit_code == 0, result.output
    assert calls.entry == ["plan"]
    payload = json.loads(result.stdout)
    assert [action["kind"] for action in payload["actions"]] == ["install", "helm-test"]
    assert "dry run" in result.stderr
    assert "owned by bootstrap" in result.stderr
    assert "dry run" not in result.stdout


@pytest.mark.parametrize("projection", ["table", "json", "yaml"])
def test_dry_run_honours_every_projection(chart_root: Path, calls: Calls, projection: str) -> None:
    result = cli("chart", "test", "alloy", "--dry-run", "-o", projection)

    assert result.exit_code == 0, result.output
    assert "helm-test" in result.stdout


def test_dry_run_takes_the_invocation_wide_output(chart_root: Path, calls: Calls) -> None:
    result = cli("-o", "yaml", "chart", "test", "alloy", "--dry-run")

    assert result.exit_code == 0, result.output


def test_output_without_dry_run_is_a_usage_error(chart_root: Path, calls: Calls) -> None:
    result = cli("chart", "test", "alloy", "-o", "json")

    assert result.exit_code == 2
    assert "--dry-run" in result.output
    assert calls.entry == []


@pytest.fixture
def hooked_repo(chart_root: Path, make_chart: MakeChart, root: Path) -> Path:
    """Two charts with hooks whose script would leave a marker if it ever ran."""
    (chart_root / "kind-config.yaml").write_text("kind: Cluster\n")
    (chart_root / ".chart-manager" / "local-cluster.yaml").write_text(LOCAL_CLUSTER)
    script = chart_root / "scripts" / "hook"
    script.parent.mkdir()
    script.write_text(f"#!/bin/sh\ntouch {chart_root / 'hook-ran'}\n")
    script.chmod(0o755)
    make_chart(
        "base",
        profiles={
            "minimal": {
                "namespace": "base",
                "hooks": {
                    "preInstall": ["scripts/hook", "--token", "s3cret"],
                    "cleanup": ["scripts/hook", "cleanup"],
                },
            }
        },
    )
    make_chart(
        "app",
        profiles={
            "minimal": {
                "namespace": "apps",
                "requires": [{"chart": "base", "profile": "minimal"}],
                "hooks": {
                    "postInstall": ["scripts/hook", "post"],
                    "cleanup": ["scripts/hook", "--token", "s3cret"],
                },
            }
        },
    )
    return chart_root


def test_dry_run_shows_redacted_hook_commands_and_runs_no_hook(hooked_repo: Path) -> None:
    table = cli("chart", "test", "app", "--dry-run", "-o", "table")
    document = cli("chart", "test", "app", "--dry-run", "-o", "json")

    assert table.exit_code == 0, table.output
    assert document.exit_code == 0, document.output
    assert not (hooked_repo / "hook-ran").exists()
    assert "scripts/hook --token ***" in table.stdout
    assert "s3cret" not in table.stdout
    actions = json.loads(document.stdout)["actions"]
    assert [(a["kind"], a["command"]) for a in actions if a["command"]] == [
        ("hook-pre-install", ["scripts/hook", "--token", "s3cret"]),
        ("hook-post-install", ["scripts/hook", "post"]),
        ("hook-cleanup", ["scripts/hook", "--token", "s3cret"]),
        ("hook-cleanup", ["scripts/hook", "cleanup"]),
    ]


def test_chart_teardown_defaults(chart_root: Path, calls: Calls) -> None:
    result = cli("chart", "teardown", "alloy")

    assert result.exit_code == 0, result.output
    assert calls.requests == [test.TeardownRequest(chart="alloy")]


def test_chart_teardown_passes_plan_options_and_keep_cluster(
    chart_root: Path, calls: Calls
) -> None:
    result = cli(
        "chart",
        "teardown",
        "alloy",
        "--profile",
        "full",
        "--namespace",
        "ns",
        "--cluster-name",
        "lab",
        "--dependent-tests",
        "--keep-cluster",
    )

    assert result.exit_code == 0, result.output
    assert calls.requests == [
        test.TeardownRequest(
            chart="alloy",
            profile="full",
            namespace="ns",
            cluster_name="lab",
            include_dependent_tests=True,
            keep_cluster=True,
        )
    ]


def test_chart_teardown_fails_naming_the_failed_cleanup_and_delete(
    chart_root: Path, calls: Calls
) -> None:
    calls.teardown_outcome = test.TeardownOutcome(
        "lab",
        cleanups=(
            ActionOutcome(
                "chart-test.alloy.minimal.hook-cleanup",
                "hook-cleanup",
                "FAIL",
                "cleanup hook exited 3: ./hook",
            ),
        ),
        delete_error="kind delete cluster failed",
    )

    result = cli("chart", "teardown", "alloy")

    message = str(result.exception)
    assert isinstance(result.exception, ChartManagerError)
    assert "chart-test.alloy.minimal.hook-cleanup" in message
    assert "cleanup hook exited 3: ./hook" in message
    assert "kind delete cluster failed" in message


@pytest.mark.parametrize("keep", [False, True])
def test_chart_teardown_dry_run_lists_redacted_cleanups_and_runs_nothing(
    hooked_repo: Path, keep: bool
) -> None:
    result = cli(
        "chart",
        "teardown",
        "app",
        "--dry-run",
        "--cluster-name",
        "lab",
        *(["--keep-cluster"] if keep else []),
    )

    assert result.exit_code == 0, result.output
    assert not (hooked_repo / "hook-ran").exists()
    assert "hook-cleanup" in result.stdout
    assert "scripts/hook --token ***" in result.stdout
    assert "s3cret" not in result.stdout
    assert "scripts/hook post" not in result.stdout
    expected = "would keep cluster lab" if keep else "would delete cluster lab"
    assert expected in result.stderr
