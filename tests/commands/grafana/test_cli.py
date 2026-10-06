"""`grafana dashboard export` and `grafana dashboard lint`: flags, output modes and exit codes."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import Result

from chart_manager.commands.grafana import dashboard_export
from chart_manager.commands.grafana.dashboard_export import ExportRequest, canonical_json
from chart_manager.commands.grafana.dashboard_lint import lint_paths
from chart_manager.commands.grafana.wire import lint_result_to_dict
from chart_manager.plumbing.yaml_files import parse_yaml
from tests.commands.grafana.conftest import PASSING_DASHBOARD as _PASSING_DASHBOARD
from tests.conftest import cli, write_workspace

# --- surface: --to is the file, -o is the format ---------------------------
#
# The rename exists for this flip. As `grafana export-dashboard`, `-o` named
# the destination *file*, so `-o json` wrote the dashboard into a file called
# `json`. `--to` now carries the destination and `-o` means what it means
# everywhere else on the surface. There is no alias -- see design doc 5.

_DASHBOARD = {
    "uid": "u",
    "title": "T",
    "schemaVersion": 39,
    "editable": True,
    "panels": [{"id": 1, "title": "p"}],
    "templating": {"list": [{"type": "datasource", "name": "DS_PROMETHEUS"}]},
}


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> list[ExportRequest]:
    """Replace `dashboard_export.export` with one that records its requests."""
    requests: list[ExportRequest] = []

    def export(request: ExportRequest, kubectl: object) -> dict[str, Any]:
        requests.append(request)
        return dict(_DASHBOARD)

    monkeypatch.setattr(dashboard_export, "export", export)
    return requests


def test_a_path_handed_to_output_is_a_usage_error_naming_to(
    exporter: list[ExportRequest],
) -> None:
    """The old spelling must fail loudly, not write a file named `charts`.

    Exit 2 is the reserved usage code, and the message has to name `--to` --
    "unknown output: charts/x.json" alone leaves the caller with a rejected
    flag and no idea where the path was supposed to go.
    """
    result = cli(
        "grafana", "dashboard", "export", "u",
        "-o", "charts/grafana-dashboards/dashboards/x.json",
    )

    assert result.exit_code == 2
    assert "--to" in result.stderr
    # Rejected at parse time, so the cluster is never contacted.
    assert exporter == []


def test_the_output_flag_still_rejects_a_plain_typo(
    exporter: list[ExportRequest],
) -> None:
    """Guard the guard: the path hint must not be the only rejection path."""
    result = cli("grafana", "dashboard", "export", "u", "-o", "jsonn")

    assert result.exit_code == 2
    assert "unknown output" in result.stderr
    assert exporter == []


def test_json_projection_is_the_canonical_document_on_stdout(
    exporter: list[ExportRequest],
) -> None:
    result = cli("grafana", "dashboard", "export", "u", "-o", "json")

    assert result.exit_code == 0
    assert result.stdout == canonical_json(_DASHBOARD)
    assert json.loads(result.stdout)["uid"] == "u"
    assert exporter[0].uid == "u"


def test_yaml_projection_is_the_same_object(exporter: list[ExportRequest]) -> None:
    result = cli("grafana", "dashboard", "export", "u", "-o", "yaml")

    assert result.exit_code == 0
    assert parse_yaml(result.stdout) == _DASHBOARD


def test_to_writes_canonical_json_and_stdout_carries_the_summary(
    tmp_path: Path, exporter: list[ExportRequest]
) -> None:
    """`-o table` is the only mode where the file and stdout coexist."""
    destination = tmp_path / "nested" / "board.json"

    result = cli(
        "grafana", "dashboard", "export", "u", "--to", str(destination), "-o", "table"
    )

    assert result.exit_code == 0
    # Missing parents are created, and the file is the git artifact.
    assert destination.read_text() == canonical_json(_DASHBOARD)
    assert "u" in result.stdout
    assert "DS_PROMETHEUS" in result.stdout
    assert str(destination) in result.stderr


def test_to_takes_the_document_so_a_json_run_leaves_stdout_empty(
    tmp_path: Path, exporter: list[ExportRequest]
) -> None:
    """The document goes to exactly one place; `--to` is that place."""
    destination = tmp_path / "board.json"

    result = cli(
        "grafana", "dashboard", "export", "u", "--to", str(destination), "-o", "json"
    )

    assert result.exit_code == 0
    assert destination.read_text() == canonical_json(_DASHBOARD)
    assert result.stdout == ""


# --- surface: what an empty target set means to a caller ------------------
#
# `lint_paths([])` is `ok` -- that is the service reporting "zero
# findings", which is true. The *command* must not report the same thing as
# success: a wrong --root or a --path that matches nothing produced a green
# CI job that linted no files at all. See design doc 8.7.


def _lint(*argv: str) -> Result:
    return cli("grafana", "dashboard", "lint", *argv)


def test_lint_dashboards_exits_nonzero_when_nothing_was_linted(
    tmp_path: Path,
) -> None:
    write_workspace(tmp_path)
    result = _lint("--root", str(tmp_path))

    assert result.exit_code == 1
    assert "no dashboards found" in result.stderr
    # The narration must not reach stdout, or a caller capturing the report
    # sees a line that is not a finding.
    assert result.stdout == ""


def test_lint_dashboards_allow_empty_opts_back_into_exit_zero(
    tmp_path: Path,
) -> None:
    write_workspace(tmp_path)
    result = _lint("--root", str(tmp_path), "--allow-empty")

    assert result.exit_code == 0
    assert "no dashboards found" in result.stderr


def test_lint_dashboards_exit_zero_still_means_a_clean_lint(
    tmp_path: Path,
) -> None:
    good = tmp_path / "good.json"
    good.write_text(_PASSING_DASHBOARD)

    result = _lint("--path", str(good))

    assert result.exit_code == 0
    assert "1 dashboards passed" in result.stderr


# --- the projections that carry the wire document ---------------------------


def test_table_projection_is_one_greppable_line_per_finding(tmp_path: Path) -> None:
    """The text projection is what a CI log grep matches, so it stays flat."""
    bad = tmp_path / "bad.json"
    bad.write_text('{"panels": [], "templating": {"list": []}}')

    result = _lint("--path", str(bad), "-o", "table")

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert lines
    # Square brackets are a rule id, not Rich markup, and nothing is wrapped.
    assert all(line.startswith(f"{bad}: [") for line in lines)
    assert any("[R002-uid]" in line for line in lines)


def test_json_and_yaml_projections_are_the_same_wire_document(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"panels": [], "templating": {"list": []}}')
    expected = lint_result_to_dict(lint_paths([bad]))

    as_json = _lint("--path", str(bad), "-o", "json")
    as_yaml = _lint("--path", str(bad), "-o", "yaml")

    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout) == expected
    assert parse_yaml(as_yaml.stdout) == expected


def test_lint_has_no_markdown_projection(tmp_path: Path) -> None:
    """`md` is offered only where a markdown projection exists (cli/output.py)."""
    good = tmp_path / "good.json"
    good.write_text(_PASSING_DASHBOARD)

    result = _lint("--path", str(good), "-o", "md")

    assert result.exit_code == 2

def test_lint_dashboards_directory_with_no_json_is_the_empty_case(tmp_path: Path) -> None:
    """An empty directory folds into "no dashboards found", not a new rule."""
    tree = tmp_path / "dashboards"
    tree.mkdir()

    result = _lint("--path", str(tree))

    assert result.exit_code == 1
    assert "no dashboards found" in result.stderr

    allowed = _lint("--path", str(tree), "--allow-empty")

    assert allowed.exit_code == 0
