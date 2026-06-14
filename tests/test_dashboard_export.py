from chart_manager.services.grafana.dashboard_export import normalize_dashboard


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
