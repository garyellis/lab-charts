from pathlib import Path

from chart_manager.services.grafana.dashboard_lint import lint_dashboard


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
