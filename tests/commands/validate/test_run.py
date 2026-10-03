"""`validate.run()`: one request in, one row per chart and environment out."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from chart_manager.commands import validate
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.yaml_files import dump_yaml
from tests.conftest import FakeCommandRunner, workspace_for


def write_chart(root: Path, name: str, **validation: Any) -> Path:
    """Write a chart whose `spec.validation` is `validation` over a `dev` default."""
    chart = root / "charts" / name
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(dump_yaml({"apiVersion": "v2", "name": name, "version": "0.1.0"}))
    (chart / "values.yaml").write_text("")
    spec = {
        "releaseName": name,
        "namespaceTemplate": "lab-${env}",
        "environments": {"dev": {"values": ["values.yaml"]}},
        **validation,
    }
    (chart / "chart-lifecycle.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "chartmanager.io/v1alpha1",
                "kind": "ChartLifecycle",
                "metadata": {"name": name},
                "spec": {"validation": spec},
            }
        )
    )
    return chart


def test_one_chart_in_one_environment_renders_into_one_passed_row(tmp_path: Path) -> None:
    write_chart(tmp_path, "demo")
    runner = FakeCommandRunner()

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",), envs=("dev",)),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    assert [(row.chart, row.env, row.checks["render"].status) for row in outcome.rows] == [
        ("demo", "dev", "passed")
    ]
    (template,) = [call for call in runner.calls if call[1] == "template"]
    assert template[2:4] == ("demo", str(tmp_path / "charts" / "demo"))
    assert ("--namespace", "lab-dev") in pairwise(template)


def test_a_helm_template_failure_fails_the_row_with_helms_error(tmp_path: Path) -> None:
    write_chart(tmp_path, "demo")
    runner = FakeCommandRunner().respond(("helm", "template"), returncode=1, stderr="bad values")

    outcome = validate.run(
        validate.ValidateRequest(charts=("demo",)),
        workspace=workspace_for(tmp_path),
        runner=runner,
    )

    (row,) = outcome.rows
    assert row.checks["render"].status == "failed"
    assert "bad values" in row.checks["render"].detail


@pytest.mark.parametrize(
    ("envs", "values"),
    [(("prod",), ["values.yaml"]), (("dev",), ["values.yaml", "missing.yaml"])],
    ids=["unknown-environment", "missing-values-file"],
)
def test_a_request_the_chart_cannot_satisfy_is_a_spec_error(
    tmp_path: Path, envs: tuple[str, ...], values: list[str]
) -> None:
    write_chart(tmp_path, "demo", environments={"dev": {"values": values}})
    runner = FakeCommandRunner()

    with pytest.raises(SpecError):
        validate.run(
            validate.ValidateRequest(charts=("demo",), envs=envs),
            workspace=workspace_for(tmp_path),
            runner=runner,
        )
    assert runner.calls == []


def test_rendering_into_a_reused_out_dir_drops_the_previous_manifests(tmp_path: Path) -> None:
    write_chart(tmp_path, "demo")
    stale = tmp_path / "out" / "demo" / "dev" / "stale.yaml"
    stale.parent.mkdir(parents=True)
    stale.write_text("kind: ConfigMap\n")

    validate.run(
        validate.ValidateRequest(charts=("demo",), out=tmp_path / "out"),
        workspace=workspace_for(tmp_path),
        runner=FakeCommandRunner(),
    )

    assert not stale.exists()
