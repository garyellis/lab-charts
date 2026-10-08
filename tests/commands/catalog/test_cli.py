"""`chart list` and `chart show`: output modes and exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.plumbing.yaml_files import parse_yaml
from tests.conftest import MakeChart, cli


def test_chart_list_yaml_carries_the_same_document(
    root: Path, make_chart: MakeChart
) -> None:
    """One document, two encoders -- `-o yaml` must not be a second shape."""
    make_chart("alloy")

    as_json = cli("chart", "list", "-o", "json")
    as_yaml = cli("chart", "list", "-o", "yaml")

    assert parse_yaml(as_yaml.stdout) == json.loads(as_json.stdout)


def test_chart_list_table_is_the_projection_a_terminal_gets(
    root: Path, make_chart: MakeChart
) -> None:
    """`-o table` keeps the human inventory: headers plus one row per chart."""
    make_chart("alloy")
    broken = make_chart("broken")
    (broken / "chart-lifecycle.yaml").write_text("version: [wrong\n", encoding="utf-8")

    result = cli("chart", "list", "-o", "table")

    assert result.exit_code == 3
    assert "Manifest validation" in result.stdout
    assert "alloy" in result.stdout
    assert "invalid" in result.stdout


def test_chart_list_off_a_terminal_resolves_auto_to_json(
    root: Path, make_chart: MakeChart
) -> None:
    """`auto` asks about stdout, and under CliRunner stdout is a pipe.

    This is what makes `chart list | jq` work in CI with no flag, and it is
    worth pinning: it means a bare `chart list` prints different things
    depending on where it prints to.
    """
    make_chart("alloy")

    result = cli("chart", "list")

    assert json.loads(result.stdout)["charts"][0]["name"] == "alloy"


def test_chart_list_json_is_the_catalog_and_a_broken_chart_exits_3(
    root: Path, make_chart: MakeChart
) -> None:
    """A jq filter reads `error`; a pipeline reads exit 3, a spec error."""
    make_chart("alloy")
    lifecycle = make_chart("other") / "chart-lifecycle.yaml"
    lifecycle.write_text(lifecycle.read_text().replace("name: other", "name: alloy"))

    result = cli("chart", "list", "-o", "json")

    assert (result.exit_code, json.loads(result.stdout)) == (
        3,
        {
            "charts": [
                {
                    "name": "alloy",
                    "version": "0.1.0",
                    "chart_type": "application",
                    "dependencies": [],
                    "lifecycle_status": "enabled",
                    "validation": "absent",
                    "chart_test": "enabled",
                    "profiles": ["minimal"],
                    "error": None,
                },
                {
                    "name": "other",
                    "version": "?",
                    "chart_type": "?",
                    "dependencies": [],
                    "lifecycle_status": "invalid",
                    "validation": "absent",
                    "chart_test": "absent",
                    "profiles": [],
                    "error": f"{lifecycle} metadata.name 'alloy' does not match chart "
                    "directory 'other' and Chart.yaml name 'other'",
                },
            ]
        },
    )


@pytest.mark.parametrize(
    "command",
    [["chart", "list"], ["chart", "show", "alloy"]],
    ids=["list", "show"],
)
def test_a_projection_neither_command_has_is_rejected_at_parse_time(
    root: Path, make_chart: MakeChart, command: list[str]
) -> None:
    """Neither has a markdown form, so `-o md` is a usage error, not a table."""
    make_chart("alloy")

    result = cli(*command, "-o", "md")

    assert result.exit_code == 2
    assert "md" in result.output


def test_chart_show_json_is_the_authored_envelope(root: Path, make_chart: MakeChart) -> None:
    """The point of `-o json` is that it can be diffed against the source."""
    make_chart("alloy")

    result = cli("chart", "show", "alloy", "-o", "json")

    profile = {
        "helmTest": True,
        "namespace": "default",
        "requires": [],
        "timeout": "10m",
        "values": ["values.yaml"],
    }
    assert (result.exit_code, json.loads(result.stdout)) == (
        0,
        {
            "apiVersion": "chartmanager.io/v1alpha1",
            "kind": "ChartLifecycle",
            "metadata": {"name": "alloy"},
            "spec": {
                "chartTest": {
                    "dependentTests": [],
                    "enabled": True,
                    "profiles": {"minimal": profile},
                },
                "enabled": True,
            },
        },
    )


def test_chart_show_table_flattens_the_envelope_onto_dotted_fields(
    root: Path, make_chart: MakeChart
) -> None:
    """`-o table` used to be unreachable: the command hardcoded JSON."""
    make_chart("alloy", profiles={"minimal": {"values": ["values.yaml"]}})

    result = cli("chart", "show", "alloy", "-o", "table")

    assert result.exit_code == 0, result.output
    assert "spec.chartTest.profiles.minimal.values" in result.stdout
    assert "values.yaml" in result.stdout
    # Leaves are spelled the way the document spells them, not the way
    # Python repr's them: `true`, never `True`.
    assert "spec.enabled" in result.stdout
    assert "True" not in result.stdout
