from pathlib import Path

from chart_manager.services.grafana.dashboard_lint import lint_dashboard, lint_paths


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
