"""`chart list` and `chart show`: output modes and exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.plumbing.exit_codes import EXIT_SPEC
from chart_manager.plumbing.yaml_files import parse_yaml
from tests.conftest import MakeChart, cli


def test_chart_list_yaml_carries_the_same_document(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """One document, two encoders -- `-o yaml` must not be a second shape."""
    make_chart("alloy")

    as_json = cli("chart", "list", "-o", "json", "--root", str(chart_root))
    as_yaml = cli("chart", "list", "-o", "yaml", "--root", str(chart_root))

    assert parse_yaml(as_yaml.stdout) == json.loads(as_json.stdout)


def test_chart_list_table_is_the_projection_a_terminal_gets(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """`-o table` keeps the human inventory: headers plus one row per chart."""
    make_chart("alloy")

    result = cli("chart", "list", "-o", "table", "--root", str(chart_root))

    assert result.exit_code == 0, result.output
    assert "Manifest validation" in result.stdout
    assert "alloy" in result.stdout


def test_chart_list_off_a_terminal_resolves_auto_to_json(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """`auto` asks about stdout, and under CliRunner stdout is a pipe.

    This is what makes `chart list | jq` work in CI with no flag, and it is
    worth pinning: it means a bare `chart list` prints different things
    depending on where it prints to.
    """
    make_chart("alloy")

    result = cli("chart", "list", "--root", str(chart_root))

    assert json.loads(result.stdout)["charts"][0]["name"] == "alloy"


@pytest.mark.parametrize("output", ["json", "table"])
def test_chart_list_reports_a_broken_chart_in_the_payload_and_the_exit_code(
    chart_root: Path, make_chart: MakeChart, output: str
) -> None:
    """A pipeline reads the exit code; a jq filter reads `error`. Both work.

    The code is 3, not 1: what failed is the *authoring* of a
    `chart-lifecycle.yaml`, which is design 6.1's spec error. `chart list`
    itself did its job and printed every row that parsed.
    """
    chart = make_chart("broken")
    (chart / "chart-lifecycle.yaml").write_text("version: [wrong\n", encoding="utf-8")

    result = cli("chart", "list", "-o", output, "--root", str(chart_root))

    assert result.exit_code == EXIT_SPEC
    assert "broken" in result.stdout
    assert "invalid" in result.stdout


@pytest.mark.parametrize(
    "command",
    [["chart", "list"], ["chart", "show", "alloy"]],
    ids=["list", "show"],
)
def test_a_projection_neither_command_has_is_rejected_at_parse_time(
    chart_root: Path, make_chart: MakeChart, command: list[str]
) -> None:
    """Neither has a markdown form, so `-o md` is a usage error, not a table."""
    make_chart("alloy")

    result = cli(*command, "-o", "md", "--root", str(chart_root))

    assert result.exit_code == 2
    assert "md" in result.output


@pytest.mark.parametrize("output", [[], ["-o", "yaml"]], ids=["auto-json", "yaml"])
def test_chart_show_prints_the_authored_envelope(
    chart_root: Path, make_chart: MakeChart, output: list[str]
) -> None:
    """The point of `-o json|yaml` is that it can be diffed against the source."""
    make_chart("alloy")

    result = cli("chart", "show", "alloy", *output, "--root", str(chart_root))

    assert result.exit_code == 0, result.output
    document = parse_yaml(result.stdout)
    assert document["apiVersion"] == "chartmanager.io/v1alpha1"
    assert document["kind"] == "ChartLifecycle"
    assert document["metadata"] == {"name": "alloy"}
    assert document["spec"]["chartTest"]["enabled"] is True


def test_chart_show_table_flattens_the_envelope_onto_dotted_fields(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """`-o table` used to be unreachable: the command hardcoded JSON."""
    make_chart("alloy", profiles={"minimal": {"values": ["values.yaml"]}})

    result = cli("chart", "show", "alloy", "-o", "table", "--root", str(chart_root))

    assert result.exit_code == 0, result.output
    assert "spec.chartTest.profiles.minimal.values" in result.stdout
    assert "values.yaml" in result.stdout
    # Leaves are spelled the way the document spells them, not the way
    # Python repr's them: `true`, never `True`.
    assert "spec.enabled" in result.stdout
    assert "True" not in result.stdout
