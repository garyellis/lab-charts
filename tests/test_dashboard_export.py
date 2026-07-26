from typing import Any

from chart_manager.services.grafana.dashboard_export import (
    ExportRequest,
    GrafanaExporter,
    canonical_json,
    normalize_dashboard,
)


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
