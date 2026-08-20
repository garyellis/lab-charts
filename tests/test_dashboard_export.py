import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from chart_manager.cli import grafana as grafana_cli
from chart_manager.services.grafana.dashboard_export import (
    ExportRequest,
    GrafanaExporter,
    canonical_json,
    normalize_dashboard,
    summarize_dashboard,
)

from .conftest import cli


def test_strips_churn_keys_and_forces_editable() -> None:
    raw = {
        "title": "T",
        "uid": "u",
        "id": 42,
        "version": 7,
        "iteration": 1700000000,
        "editable": False,
        "panels": [],
    }

    out = normalize_dashboard(raw)

    assert "id" not in out
    assert "version" not in out
    assert "iteration" not in out
    assert out["editable"] is True
    assert out["title"] == "T"
    assert out["uid"] == "u"


def test_rewrites_live_datasource_uids_to_template_vars() -> None:
    raw = {
        "title": "T",
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "datasource": {"type": "prometheus", "uid": "mimir"},
                "targets": [
                    {"datasource": {"type": "loki", "uid": "loki"}, "expr": "x"},
                ],
            },
            {
                "id": 2,
                "type": "trace",
                "datasource": {"type": "tempo", "uid": "tempo"},
            },
        ],
    }

    out = normalize_dashboard(raw)

    assert out["panels"][0]["datasource"] == {
        "type": "prometheus",
        "uid": "${DS_PROMETHEUS}",
    }
    assert out["panels"][0]["targets"][0]["datasource"] == {
        "type": "loki",
        "uid": "${DS_LOKI}",
    }
    assert out["panels"][1]["datasource"] == {"type": "tempo", "uid": "${DS_TEMPO}"}
    # Non-datasource object fields are preserved on rewrites.
    assert out["panels"][0]["type"] == "timeseries"
    assert out["panels"][0]["id"] == 1


def test_rewrites_the_obs_w_thanos_datasource_uid() -> None:
    out = normalize_dashboard(
        {
            "panels": [
                {
                    "datasource": {"type": "prometheus", "uid": "thanos"},
                    "id": 1,
                    "type": "timeseries",
                }
            ]
        }
    )

    assert out["panels"][0]["datasource"] == {
        "type": "prometheus",
        "uid": "${DS_PROMETHEUS}",
    }


def test_unknown_datasource_uid_is_left_alone() -> None:
    raw = {
        "title": "T",
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "datasource": {"type": "prometheus", "uid": "some-other-ds"},
            }
        ],
    }

    out = normalize_dashboard(raw)

    assert out["panels"][0]["datasource"] == {
        "type": "prometheus",
        "uid": "some-other-ds",
    }


def test_normalize_does_not_mutate_input() -> None:
    raw = {"title": "T", "id": 1, "editable": False, "version": 9}
    before = dict(raw)

    normalize_dashboard(raw)

    assert raw == before


# ----- canonical_json -------------------------------------------------------
#
# The git-normalization contract (sorted keys, 2-space indent, trailing
# newline) used to live in `cli/main.py`'s export handler. It has to be
# owned by the exporter so a committed dashboard and a fresh export of the
# same dashboard are byte-identical no matter which surface wrote it.


def test_canonical_json_sorts_keys_and_indents_two_spaces() -> None:
    payload = canonical_json({"uid": "u", "editable": True, "annotations": {"b": 1, "a": 2}})

    assert payload == (
        "{\n"
        '  "annotations": {\n'
        '    "a": 2,\n'
        '    "b": 1\n'
        "  },\n"
        '  "editable": true,\n'
        '  "uid": "u"\n'
        "}\n"
    )


def test_canonical_json_ends_with_exactly_one_newline() -> None:
    payload = canonical_json({"uid": "u"})

    assert payload.endswith("}\n")
    assert not payload.endswith("}\n\n")


def test_canonical_json_is_stable_across_key_insertion_order() -> None:
    a = canonical_json({"uid": "u", "title": "T"})
    b = canonical_json({"title": "T", "uid": "u"})

    assert a == b


def test_export_returns_the_canonical_payload_of_the_normalized_dashboard() -> None:
    class _StubExporter(GrafanaExporter):
        def fetch(self, request: ExportRequest) -> dict[str, Any]:
            return {"uid": "u", "editable": True}

    # `fetch` is overridden, so no kubectl call is reachable -- this test
    # covers the export -> canonical_json seam only.
    payload = _StubExporter(kubectl=None).export(  # type: ignore[arg-type]
        ExportRequest(uid="u", cluster_name="c", namespace="observability")
    )

    assert payload == canonical_json({"uid": "u", "editable": True})


# ----- summarize_dashboard --------------------------------------------------
#
# `-o table` needs a human projection of a document that has no table form,
# so the fields it shows are a service decision rather than a CLI one.


def test_summary_reports_the_identifying_fields() -> None:
    summary = summarize_dashboard(
        {
            "uid": "u",
            "title": "T",
            "schemaVersion": 39,
            "panels": [{"id": 1}, {"id": 2, "type": "row", "panels": [{"id": 3}]}],
            "templating": {
                "list": [
                    {"type": "datasource", "name": "DS_PROMETHEUS"},
                    {"type": "query", "name": "namespace"},
                    {"type": "datasource", "name": "DS_LOKI"},
                ]
            },
        }
    )

    assert summary.uid == "u"
    assert summary.title == "T"
    assert summary.schema_version == 39
    # The row's nested panel is the row's content, not a third top-level panel.
    assert summary.top_level_panels == 2
    assert summary.datasource_variables == ("DS_PROMETHEUS", "DS_LOKI")


def test_summary_tolerates_a_dashboard_missing_every_optional_field() -> None:
    """A malformed dashboard is a lint finding, never an export crash."""
    summary = summarize_dashboard({})

    assert summary == summarize_dashboard({"schemaVersion": "38"})
    assert summary.uid == ""
    assert summary.title == ""
    assert summary.schema_version is None
    assert summary.top_level_panels == 0
    assert summary.datasource_variables == ()


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
    """Wire a fetch-only exporter into `grafana._container()`; return its requests."""
    requests: list[ExportRequest] = []

    def fetch(request: ExportRequest) -> dict[str, Any]:
        requests.append(request)
        return dict(_DASHBOARD)

    monkeypatch.setattr(
        grafana_cli,
        "_container",
        lambda: SimpleNamespace(
            grafana_exporter=lambda: SimpleNamespace(fetch=fetch),
        ),
    )
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
    assert yaml.safe_load(result.stdout) == _DASHBOARD


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
