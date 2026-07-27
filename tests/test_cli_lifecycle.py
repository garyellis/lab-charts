"""CLI contract tests for ``chart-manager lifecycle``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
import yaml
from typer.testing import CliRunner

from chart_manager.cli import lifecycle as lifecycle_cli
from chart_manager.cli.main import app
from chart_manager.plumbing.errors import SpecError


def _build_app() -> typer.Typer:
    lifecycle = typer.Typer(no_args_is_help=True)
    lifecycle_cli.register(lifecycle)
    result = typer.Typer()
    result.add_typer(lifecycle, name="lifecycle")
    return result


@dataclass
class _Plan:
    payload: dict[str, Any]
    actions: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.payload


class _Compiler:
    def __init__(self, plan: _Plan) -> None:
        self.plan = plan
        self.calls: list[tuple[str, str, str]] = []

    def compile_validation(self, chart: str, environment: str) -> _Plan:
        self.calls.append(("validation", chart, environment))
        return self.plan

    def compile_cluster_test(
        self,
        chart: str,
        profile: str,
        default_namespace: str = "default",
    ) -> _Plan:
        self.calls.append(("cluster-test", chart, profile))
        return self.plan


def _sample_plan() -> _Plan:
    return _Plan(
        {
            "workflow": "validation",
            "chart": "grafana",
            "environment": "dev",
            "actions": [
                {
                    "actionId": "grafana.dev.render",
                    "kind": "render",
                    "target": {"chart": "grafana", "environment": "dev"},
                    "inputDigest": "sha256:one",
                    "chartPath": "charts/grafana",
                },
                {
                    "actionId": "grafana.dev.schema",
                    "kind": "schema-validate",
                    "target": {"chart": "grafana", "environment": "dev"},
                    "inputDigest": "sha256:two",
                    "chartPath": "charts/grafana",
                },
            ],
            "edges": [
                {
                    "source": "grafana.dev.render",
                    "target": "grafana.dev.schema",
                    "kind": "input",
                }
            ],
        }
    )


def test_main_registers_lifecycle_group() -> None:
    result = CliRunner().invoke(app, ["lifecycle", "--help"])

    assert result.exit_code == 0
    assert "plan" in result.stdout
    assert "graph" in result.stdout
    assert "doctor" in result.stdout
    assert "status" in result.stdout
    assert "impact" in result.stdout


def test_plan_json_dispatches_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compiler = _Compiler(_sample_plan())
    monkeypatch.setattr(lifecycle_cli, "_compiler", lambda root: compiler)

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "plan",
            "grafana",
            "--workflow",
            "validation",
            "--profile",
            "dev",
            "--format",
            "json",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["actions"][0]["kind"] == "render"
    assert compiler.calls == [("validation", "grafana", "dev")]


def test_plan_dispatches_cluster_test(monkeypatch: pytest.MonkeyPatch) -> None:
    compiler = _Compiler(_sample_plan())
    monkeypatch.setattr(lifecycle_cli, "_compiler", lambda root: compiler)

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "plan",
            "grafana",
            "--workflow",
            "cluster-test",
            "--profile",
            "minimal",
        ],
    )

    assert result.exit_code == 0
    assert compiler.calls == [("cluster-test", "grafana", "minimal")]


def test_invalid_workflow_is_rejected_before_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle_cli,
        "_compiler",
        lambda root: pytest.fail("compiler should not be constructed"),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "plan",
            "grafana",
            "--workflow",
            "deploy",
            "--profile",
            "dev",
        ],
    )

    assert result.exit_code == 2
    assert "--workflow" in result.stderr
    assert "validation, cluster-test" in result.stderr


def test_graph_text_displays_compact_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    compiler = _Compiler(_sample_plan())
    monkeypatch.setattr(lifecycle_cli, "_compiler", lambda root: compiler)

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "graph",
            "grafana",
            "--workflow",
            "validation",
            "--profile",
            "dev",
        ],
    )

    assert result.exit_code == 0
    assert "grafana.dev.render -> grafana.dev.schema [input]" in result.stdout


def test_graph_json_omits_execution_details(monkeypatch: pytest.MonkeyPatch) -> None:
    compiler = _Compiler(_sample_plan())
    monkeypatch.setattr(lifecycle_cli, "_compiler", lambda root: compiler)

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "graph",
            "grafana",
            "--workflow",
            "validation",
            "--profile",
            "dev",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["actions"][0] == {
        "actionId": "grafana.dev.render",
        "kind": "render",
        "target": {"chart": "grafana", "environment": "dev"},
    }
    assert "inputDigest" not in payload["actions"][0]
    assert payload["edges"][0]["kind"] == "input"


def test_doctor_json_exits_nonzero_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    report = SimpleNamespace(
        ok=False,
        diagnostics=(
            SimpleNamespace(
                severity="error",
                chart="grafana",
                message="unknown profile reference",
            ),
        ),
        to_dict=lambda: {
            "ok": False,
            "checkedCharts": 1,
            "diagnostics": [
                {
                    "severity": "error",
                    "chart": "grafana",
                    "message": "unknown profile reference",
                }
            ],
        },
    )
    monkeypatch.setattr(lifecycle_cli, "_doctor", lambda root: report)

    result = CliRunner().invoke(
        _build_app(),
        ["lifecycle", "doctor", "--format", "json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["ok"] is False


@pytest.mark.parametrize(
    ("workflow", "expected_profile"),
    [("validation", "dev"), ("cluster-test", "minimal")],
)
def test_status_uses_workflow_default_profile(
    monkeypatch: pytest.MonkeyPatch,
    workflow: str,
    expected_profile: str,
) -> None:
    plan = _Plan({"actions": [], "edges": []})
    compiler = _Compiler(plan)
    monkeypatch.setattr(lifecycle_cli, "_compiler", lambda root: compiler)
    projected: list[Any] = []
    result_object = SimpleNamespace(
        to_dict=lambda: {
            "actions": [],
            "conditions": [],
            "diagnostics": [],
        }
    )
    monkeypatch.setattr(
        lifecycle_cli,
        "_status_service",
        lambda root: SimpleNamespace(
            project=lambda value, observers=(): projected.append((value, observers))
            or result_object
        ),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "status",
            "grafana",
            "--workflow",
            workflow,
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert compiler.calls == [(workflow, "grafana", expected_profile)]
    assert projected == [(plan, ())]
    assert json.loads(result.stdout)["actions"] == []


def test_status_live_is_rejected_for_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lifecycle_cli,
        "_compiler",
        lambda root: pytest.fail("validation should fail before compilation"),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "status",
            "grafana",
            "--workflow",
            "validation",
            "--live",
        ],
    )

    assert result.exit_code == 2
    assert "--live is only supported for the cluster-test workflow" in result.stderr


def test_cluster_status_passes_live_observers_to_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _Plan({"actions": [], "edges": []})
    compiler = _Compiler(plan)
    monkeypatch.setattr(lifecycle_cli, "_compiler", lambda root: compiler)
    built: list[tuple[str, str | None]] = []
    observers = (object(), object())
    monkeypatch.setattr(
        lifecycle_cli,
        "_build_live_observers",
        lambda *, cluster_name, kube_context: built.append(
            (cluster_name, kube_context)
        )
        or lifecycle_cli._LiveObserverBundle(observers, []),
    )
    projected: list[tuple[Any, tuple[Any, ...]]] = []
    result_object = SimpleNamespace(
        to_dict=lambda: {"actions": [], "conditions": [], "diagnostics": []}
    )
    monkeypatch.setattr(
        lifecycle_cli,
        "_status_service",
        lambda root: SimpleNamespace(
            project=lambda value, *, observers: projected.append((value, observers))
            or result_object
        ),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "status",
            "grafana",
            "--workflow",
            "cluster-test",
            "--live",
            "--cluster-name",
            "observability",
            "--context",
            "kind-observability-alt",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert built == [("observability", "kind-observability-alt")]
    assert projected == [(plan, observers)]


def test_live_builder_domain_error_degrades_to_cached_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _Plan({"actions": [], "edges": []})
    compiler = _Compiler(plan)
    monkeypatch.setattr(lifecycle_cli, "_compiler", lambda root: compiler)

    def _unavailable(**kwargs: Any) -> Any:
        raise SpecError("cannot reach cluster")

    monkeypatch.setattr(lifecycle_cli, "_build_live_observers", _unavailable)
    result_object = SimpleNamespace(
        to_dict=lambda: {"actions": [], "conditions": [], "diagnostics": []}
    )
    monkeypatch.setattr(
        lifecycle_cli,
        "_status_service",
        lambda root: SimpleNamespace(
            project=lambda value, *, observers: result_object
        ),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "status",
            "grafana",
            "--workflow",
            "cluster-test",
            "--live",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["actions"] == []
    assert "warning: live observation unavailable: cannot reach cluster" in result.stderr


def test_safe_live_observer_stops_querying_after_domain_error() -> None:
    calls: list[Any] = []

    def _raise(action: Any) -> Any:
        calls.append(action)
        raise SpecError("cluster is unavailable")

    warnings: list[str] = []
    observer = lifecycle_cli._SafeLiveObserver(
        SimpleNamespace(observe=_raise),
        warnings,
    )
    action = object()

    assert observer.observe(action) is None
    assert observer.observe(action) is None
    assert calls == [action]
    assert warnings == ["live observation unavailable: cluster is unavailable"]


def test_status_yaml_is_kubernetes_shaped(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _Plan({"actions": [], "edges": []})
    monkeypatch.setattr(lifecycle_cli, "_compiler", lambda root: _Compiler(plan))
    result_object = SimpleNamespace(
        to_dict=lambda: {
            "apiVersion": "lifecycle.cmg.io/v1alpha1",
            "kind": "LifecycleStatus",
            "actions": [],
            "conditions": [
                {
                    "type": "render",
                    "status": "UNKNOWN",
                    "reason": "NoEvidence",
                }
            ],
            "diagnostics": [],
        }
    )
    monkeypatch.setattr(
        lifecycle_cli,
        "_status_service",
        lambda root: SimpleNamespace(
            project=lambda value, *, observers: result_object
        ),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "status",
            "grafana",
            "--workflow",
            "validation",
            "--profile",
            "dev",
            "--format",
            "yaml",
        ],
    )

    assert result.exit_code == 0
    payload = yaml.safe_load(result.stdout)
    assert payload["apiVersion"] == "lifecycle.cmg.io/v1alpha1"
    assert payload["kind"] == "LifecycleStatus"
    assert payload["conditions"][0]["status"] == "UNKNOWN"


def _impact_result(
    *,
    spec_errors: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> Any:
    reason = SimpleNamespace(
        code="validation-trigger",
        changed_file=Path("charts/grafana/values-dev.yaml"),
        detail="authored validation trigger selected grafana",
    )
    validation = SimpleNamespace(
        chart="grafana",
        environment="dev",
        reasons=(reason,),
    )
    cluster = SimpleNamespace(
        chart="grafana",
        profile="minimal",
        reasons=(
            SimpleNamespace(
                code="chart-change",
                changed_file=Path("charts/grafana/values-dev.yaml"),
                detail="changed file belongs to grafana",
            ),
        ),
    )
    payload = {
        "apiVersion": "lifecycle.cmg.io/v1alpha1",
        "kind": "LifecycleImpact",
        "changedFiles": [
            "charts/grafana/values-dev.yaml",
            "kind-config.yaml",
        ],
        "validationSelection": [
            {
                "chart": "grafana",
                "environment": "dev",
                "reasons": [
                    {
                        "code": "validation-trigger",
                        "changedFile": "charts/grafana/values-dev.yaml",
                        "detail": "authored validation trigger selected grafana",
                    }
                ],
            }
        ],
        "clusterTestMatrix": [
            {
                "chart": "grafana",
                "profile": "minimal",
                "reasons": [
                    {
                        "code": "chart-change",
                        "changedFile": "charts/grafana/values-dev.yaml",
                        "detail": "changed file belongs to grafana",
                    }
                ],
            }
        ],
        "specErrors": list(spec_errors),
        "warnings": list(warnings),
    }
    return SimpleNamespace(
        validation=(validation,),
        cluster_tests=(cluster,),
        spec_errors=spec_errors,
        warnings=warnings,
        to_dict=lambda: payload,
    )


def test_impact_combines_changed_file_sources_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    changed_files = tmp_path / "changes.txt"
    changed_files.write_text(
        "\ncharts/grafana/values-dev.yaml\n\n",
        encoding="utf-8",
    )
    captured: list[list[str]] = []
    result_object = _impact_result()
    monkeypatch.setattr(
        lifecycle_cli,
        "_impact_service",
        lambda root: SimpleNamespace(
            analyze=lambda changes: captured.append(changes) or result_object
        ),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "impact",
            "--changed-files",
            str(changed_files),
            "--changed-file",
            "kind-config.yaml",
            "--format",
            "json",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured == [
        ["charts/grafana/values-dev.yaml", "kind-config.yaml"]
    ]
    assert json.loads(result.stdout)["kind"] == "LifecycleImpact"


def test_impact_requires_an_explicit_change_source() -> None:
    result = CliRunner().invoke(
        _build_app(),
        ["lifecycle", "impact"],
    )

    assert result.exit_code == 2
    assert "--changed-files / --changed-file" in result.stderr


def test_impact_changed_files_read_error_is_bad_parameter(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"

    result = CliRunner().invoke(
        _build_app(),
        ["lifecycle", "impact", "--changed-files", str(missing)],
    )

    assert result.exit_code == 2
    assert "--changed-files" in result.stderr
    assert "cannot read changed-files input" in result.stderr


def test_impact_text_shows_reasons_warnings_and_exits_on_spec_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_object = _impact_result(
        spec_errors=("grafana: unknown environment prod",),
        warnings=("unmatched chart change README.md",),
    )
    monkeypatch.setattr(
        lifecycle_cli,
        "_impact_service",
        lambda root: SimpleNamespace(analyze=lambda changes: result_object),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "impact",
            "--changed-file",
            "charts/grafana/values-dev.yaml",
        ],
    )

    assert result.exit_code == 1
    assert "Validation:" in result.stdout
    assert "grafana/dev" in result.stdout
    assert "validation-trigger: charts/grafana/values-dev.yaml" in result.stdout
    assert "Cluster tests:" in result.stdout
    assert "grafana/minimal" in result.stdout
    assert "Warnings:" in result.stdout
    assert "unmatched chart change README.md" in result.stdout
    assert "Spec errors:" in result.stdout
    assert "grafana: unknown environment prod" in result.stdout


def test_impact_yaml_preserves_machine_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_object = _impact_result()
    monkeypatch.setattr(
        lifecycle_cli,
        "_impact_service",
        lambda root: SimpleNamespace(analyze=lambda changes: result_object),
    )

    result = CliRunner().invoke(
        _build_app(),
        [
            "lifecycle",
            "impact",
            "--changed-file",
            "charts/grafana/values-dev.yaml",
            "--format",
            "yaml",
        ],
    )

    assert result.exit_code == 0
    payload = yaml.safe_load(result.stdout)
    assert payload["apiVersion"] == "lifecycle.cmg.io/v1alpha1"
    assert payload["kind"] == "LifecycleImpact"
    assert payload["clusterTestMatrix"][0]["profile"] == "minimal"
