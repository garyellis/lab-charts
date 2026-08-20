import json
from pathlib import Path

import yaml
from typer.testing import Result

from chart_manager.services.grafana.dashboard_lint import (
    expand_targets,
    lint_dashboard,
    lint_paths,
    rendered_configmap_name,
)
from chart_manager.services.grafana.wire import SCHEMA_VERSION, lint_result_to_dict

from .conftest import cli

#: A dashboard that satisfies every rule, for the "exit 0 still means clean"
#: case below.
_PASSING_DASHBOARD = """{
  "title": "T", "uid": "u", "schemaVersion": 38, "editable": true,
  "panels": [{"id": 1, "title": "p",
              "datasource": {"type":"prometheus","uid":"${DS_PROMETHEUS}"},
              "targets":[{"expr":"rate(x[$__rate_interval])"}]}],
  "templating": {"list":[{"type":"datasource","name":"DS_PROMETHEUS"}]}
}"""


def test_passing(tmp_path: Path) -> None:
    p = tmp_path / "ok.json"
    p.write_text(
        """{
      "title": "T", "uid": "u", "schemaVersion": 38, "editable": true,
      "panels": [{"id": 1, "title": "p",
                  "datasource": {"type":"prometheus","uid":"${DS_PROMETHEUS}"},
                  "targets":[{"expr":"rate(x[$__rate_interval])"}]}],
      "templating": {"list":[{"type":"datasource","name":"DS_PROMETHEUS"}]}
    }"""
    )
    assert lint_dashboard(p) == []


def test_hardcoded_rate_and_missing_uid(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(
        """{
      "title": "T", "schemaVersion": 38,
      "panels": [{"id": 1, "type":"timeseries", "title": "p",
                  "datasource":{"type":"prometheus","uid":"x"},
                  "targets":[{"expr":"rate(http_requests_total[1m])"}]}],
      "templating": {"list":[]}
    }"""
    )
    rules = {f.rule for f in lint_dashboard(p)}
    assert "R002-uid" in rules
    assert "R006-rate-interval" in rules
    assert "R007-templated-ds" in rules


def test_text_panel_does_not_require_datasource(tmp_path: Path) -> None:
    p = tmp_path / "text.json"
    p.write_text(
        """{
      "title": "T", "uid": "u", "schemaVersion": 38, "editable": true,
      "panels": [{"id": 1, "type": "text", "title": "intro"}],
      "templating": {"list":[{"type":"datasource","name":"DS_PROMETHEUS"}]}
    }"""
    )
    rules = {f.rule for f in lint_dashboard(p)}
    assert "R005-panel-datasource" not in rules


# ----- LintResult -----------------------------------------------------------
#
# The pass/fail rule and the "N findings across M/N dashboards" tally used
# to be inline in `cli/main.py`. They now come off the result so any surface
# reports the same verdict.


def test_lint_result_is_ok_when_every_dashboard_passes(tmp_path: Path) -> None:
    good = tmp_path / "ok.json"
    good.write_text(
        """{
      "title": "T", "uid": "u", "schemaVersion": 38, "editable": true,
      "panels": [],
      "templating": {"list":[{"type":"datasource","name":"DS_PROMETHEUS"}]}
    }"""
    )

    result = lint_paths([good])

    assert result.ok
    assert result.findings == ()
    assert result.files_scanned == 1
    assert result.files_with_findings == 0


def test_lint_result_counts_files_not_findings(tmp_path: Path) -> None:
    # One clean file, one file with several findings: files_scanned counts
    # everything, files_with_findings counts only the offender.
    good = tmp_path / "ok.json"
    good.write_text(
        """{
      "title": "T", "uid": "u", "schemaVersion": 38, "editable": true,
      "panels": [],
      "templating": {"list":[{"type":"datasource","name":"DS_PROMETHEUS"}]}
    }"""
    )
    bad = tmp_path / "bad.json"
    bad.write_text('{"panels": [], "templating": {"list": []}}')

    result = lint_paths([good, bad])

    assert not result.ok
    assert result.files_scanned == 2
    assert result.files_with_findings == 1
    assert {f.path for f in result.findings} == {bad}


def test_lint_result_on_empty_target_list_is_ok(tmp_path: Path) -> None:
    result = lint_paths([])

    assert result.ok
    assert result.files_scanned == 0
    assert result.files_with_findings == 0


def test_expand_targets_passes_a_missing_file_through_untouched(tmp_path: Path) -> None:
    """"You named a file that is not there" must stay its own diagnostic.

    Swallowing it here would turn `--path typo.json` into "no dashboards
    found", which names neither the typo nor the file.
    """
    missing = tmp_path / "gone.json"

    assert expand_targets([missing]) == [missing]

# --- surface: what an empty target set means to a caller ------------------
#
# `lint_paths([])` is `ok` above -- that is the service reporting "zero
# findings", which is true. The *command* must not report the same thing as
# success: a wrong --root or a --path that matches nothing produced a green
# CI job that linted no files at all. See design doc 8.7.


def _lint(*argv: str) -> Result:
    return cli("grafana", "dashboard", "lint", *argv)


def test_lint_dashboards_exits_nonzero_when_nothing_was_linted(
    tmp_path: Path,
) -> None:
    result = _lint("--root", str(tmp_path))

    assert result.exit_code == 1
    assert "no dashboards found" in result.stderr
    # The narration must not reach stdout, or a caller capturing the report
    # sees a line that is not a finding.
    assert result.stdout == ""


def test_lint_dashboards_allow_empty_opts_back_into_exit_zero(
    tmp_path: Path,
) -> None:
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


# --- the wire contract and the projections that carry it -------------------


def test_wire_payload_carries_the_tally_as_well_as_the_findings(
    tmp_path: Path,
) -> None:
    """`files_scanned` is not derivable from an empty `findings` list.

    A clean run over 40 dashboards and a run that found no dashboards at all
    both produce no findings; the tally is what separates them, which is the
    distinction design doc 8.7 is about.
    """
    bad = tmp_path / "bad.json"
    bad.write_text('{"panels": [], "templating": {"list": []}}')

    payload = lint_result_to_dict(lint_paths([bad]))

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["ok"] is False
    assert payload["files_scanned"] == 1
    assert payload["files_with_findings"] == 1
    assert {finding["rule"] for finding in payload["findings"]} >= {"R001-title", "R002-uid"}
    assert {finding["path"] for finding in payload["findings"]} == {bad.as_posix()}


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
    assert yaml.safe_load(as_yaml.stdout) == expected


def test_lint_has_no_markdown_projection(tmp_path: Path) -> None:
    """`md` is offered only where a markdown projection exists (cli/output.py)."""
    good = tmp_path / "good.json"
    good.write_text(_PASSING_DASHBOARD)

    result = _lint("--path", str(good), "-o", "md")

    assert result.exit_code == 2

# --- surface: --path DIR is a linted tree, not a traceback -----------------
#
# Design doc 8.9. `--path some/dir/` reached `Path.read_text` and killed the
# process with a raw `IsADirectoryError`. A directory is the natural thing to
# hand this flag, so it now means what the default discovery means: lint the
# JSON under it, recursively.


def test_lint_dashboards_recurses_into_a_directory_path(tmp_path: Path) -> None:
    tree = tmp_path / "dashboards"
    (tree / "nested").mkdir(parents=True)
    (tree / "a.json").write_text(_PASSING_DASHBOARD)
    (tree / "nested" / "b.json").write_text(
        _PASSING_DASHBOARD.replace('"uid": "u"', '"uid": "nested-u"')
    )
    (tree / "notes.txt").write_text("not a dashboard")

    result = _lint("--path", str(tree))

    assert result.exit_code == 0, result.output
    assert "2 dashboards passed" in result.stderr


def test_lint_dashboards_reports_findings_from_a_directory_path(tmp_path: Path) -> None:
    """Guard the guard: the directory really is linted, not merely counted."""
    tree = tmp_path / "dashboards"
    tree.mkdir()
    (tree / "bad.json").write_text('{"panels": [], "templating": {"list": []}}')

    result = _lint("--path", str(tree))

    assert result.exit_code == 1
    assert "R001-title" in result.stdout


def test_lint_dashboards_directory_with_no_json_is_the_empty_case(tmp_path: Path) -> None:
    """An empty directory folds into "no dashboards found", not a new rule."""
    tree = tmp_path / "dashboards"
    tree.mkdir()

    result = _lint("--path", str(tree))

    assert result.exit_code == 1
    assert "no dashboards found" in result.stderr

    allowed = _lint("--path", str(tree), "--allow-empty")

    assert allowed.exit_code == 0


def test_a_binary_file_is_a_finding_and_not_a_decode_traceback(tmp_path: Path) -> None:
    """`UnicodeDecodeError` is the same event as malformed JSON: R000."""
    blob = tmp_path / "blob.json"
    blob.write_bytes(b"\xff\xfe\x00binary")

    findings = lint_dashboard(blob)

    assert [f.rule for f in findings] == ["R000-json"]


def test_lint_rejects_oversize_dashboard_payload(tmp_path: Path) -> None:
    dashboard = tmp_path / "large.json"
    payload = json.loads(_PASSING_DASHBOARD)
    payload["description"] = "x" * (900 * 1024)
    dashboard.write_text(json.dumps(payload))

    assert "R008-size" in {finding.rule for finding in lint_dashboard(dashboard)}


def test_lint_rejects_hard_coded_datasource_uid(tmp_path: Path) -> None:
    dashboard = tmp_path / "hard-coded.json"
    payload = json.loads(_PASSING_DASHBOARD)
    payload["panels"][0]["datasource"]["uid"] = "thanos"
    dashboard.write_text(json.dumps(payload))

    assert "R009-datasource-uid" in {
        finding.rule for finding in lint_dashboard(dashboard)
    }


def test_lint_rejects_unsupported_dashboard_url(tmp_path: Path) -> None:
    dashboard = tmp_path / "unsafe-url.json"
    payload = json.loads(_PASSING_DASHBOARD)
    payload["links"] = [{"title": "unsafe", "url": "javascript:alert(1)"}]
    dashboard.write_text(json.dumps(payload))

    assert "R010-url" in {finding.rule for finding in lint_dashboard(dashboard)}


def test_lint_rejects_plain_http_dashboard_url(tmp_path: Path) -> None:
    dashboard = tmp_path / "plain-http.json"
    payload = json.loads(_PASSING_DASHBOARD)
    payload["links"] = [{"title": "insecure", "url": "http://grafana.example.test"}]
    dashboard.write_text(json.dumps(payload))

    assert "R010-url" in {finding.rule for finding in lint_dashboard(dashboard)}


def test_lint_paths_rejects_duplicate_uids(tmp_path: Path) -> None:
    first = tmp_path / "one" / "a.json"
    second = tmp_path / "two" / "b.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(_PASSING_DASHBOARD)
    second.write_text(_PASSING_DASHBOARD)

    result = lint_paths([first, second])

    assert "R011-duplicate-uid" in {finding.rule for finding in result.findings}


def test_rendered_name_is_group_qualified_and_bounded(tmp_path: Path) -> None:
    dashboard = (
        tmp_path
        / "ai1-openstack"
        / "a-dashboard-name-long-enough-to-require-truncation.json"
    )

    name = rendered_configmap_name(dashboard)

    assert name.startswith("grafana-dashboard-ai1-openstack-")
    assert len(name) <= 63
