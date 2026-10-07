"""The dashboard lint rules, the run's tally and its wire document."""

import json
from pathlib import Path
from typing import Any

import pytest

from chart_manager.commands.grafana.dashboard_lint import (
    expand_targets,
    lint_dashboard,
    lint_paths,
    rendered_configmap_name,
)
from chart_manager.commands.grafana.wire import lint_result_to_dict
from tests.conftest import PASSING_DASHBOARD


def test_passing(tmp_path: Path) -> None:
    p = tmp_path / "ok.json"
    p.write_text(PASSING_DASHBOARD)
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
# The pass/fail rule and the "N findings across M/N dashboards" tally come
# off the result so any surface reports the same verdict.


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


def test_expand_targets_recurses_into_a_directory(tmp_path: Path) -> None:
    """`--path DIR` lints the JSON under it, as the default discovery does."""
    tree = tmp_path / "dashboards"
    (tree / "nested").mkdir(parents=True)
    (tree / "a.json").write_text(PASSING_DASHBOARD)
    (tree / "nested" / "b.json").write_text(PASSING_DASHBOARD)
    (tree / "notes.txt").write_text("not a dashboard")

    assert expand_targets([tree]) == [tree / "a.json", tree / "nested" / "b.json"]


def test_wire_payload_carries_the_tally_as_well_as_the_findings(
    tmp_path: Path,
) -> None:
    """`files_scanned` is not derivable from an empty `findings` list.

    A clean run over 40 dashboards and a run that found no dashboards at all
    both produce no findings; the tally is what separates them, which is the
    distinction the tally exists for.
    """
    bad = tmp_path / "bad.json"
    bad.write_text('{"panels": [], "templating": {"list": []}}')

    payload = lint_result_to_dict(lint_paths([bad]))

    assert payload["ok"] is False
    assert payload["files_scanned"] == 1
    assert payload["files_with_findings"] == 1
    assert {finding["rule"] for finding in payload["findings"]} >= {"R001-title", "R002-uid"}
    assert {finding["path"] for finding in payload["findings"]} == {bad.as_posix()}


def test_a_binary_file_is_a_finding_and_not_a_decode_traceback(tmp_path: Path) -> None:
    """`UnicodeDecodeError` is the same event as malformed JSON: R000."""
    blob = tmp_path / "blob.json"
    blob.write_bytes(b"\xff\xfe\x00binary")

    findings = lint_dashboard(blob)

    assert [f.rule for f in findings] == ["R000-json"]


def _oversize(payload: dict[str, Any]) -> None:
    payload["description"] = "x" * (900 * 1024)


def _hard_coded_uid(payload: dict[str, Any]) -> None:
    payload["panels"][0]["datasource"]["uid"] = "thanos"


def _link(url: str) -> Any:
    return lambda payload: payload.update(links=[{"title": "l", "url": url}])


@pytest.mark.parametrize(
    ("edit", "rule"),
    [
        (_oversize, "R008-size"),
        (_hard_coded_uid, "R009-datasource-uid"),
        (_link("javascript:alert(1)"), "R010-url"),
        (_link("http://grafana.example.test"), "R010-url"),
    ],
    ids=["oversize", "hard-coded-datasource-uid", "unsupported-url", "plain-http-url"],
)
def test_lint_rejects(tmp_path: Path, edit: Any, rule: str) -> None:
    dashboard = tmp_path / "dashboard.json"
    payload = json.loads(PASSING_DASHBOARD)
    edit(payload)
    dashboard.write_text(json.dumps(payload))

    assert rule in {finding.rule for finding in lint_dashboard(dashboard)}


def test_lint_paths_rejects_duplicate_uids(tmp_path: Path) -> None:
    first = tmp_path / "one" / "a.json"
    second = tmp_path / "two" / "b.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(PASSING_DASHBOARD)
    second.write_text(PASSING_DASHBOARD)

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
