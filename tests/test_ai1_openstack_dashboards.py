import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "grafana-dashboards"
GROUP = CHART / "dashboards" / "ai1-openstack"


def dashboards() -> list[dict[str, Any]]:
    """Load the complete initial ai1/OpenStack dashboard slice."""
    return [json.loads(path.read_text()) for path in sorted(GROUP.glob("*.json"))]


def objects(node: Any) -> list[dict[str, Any]]:
    """Flatten dashboard objects for contract assertions over nested fields."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(objects(value))
    return found


def expressions(dashboard: dict[str, Any]) -> str:
    """Join PromQL expressions for concise coverage-contract assertions."""
    return "\n".join(obj["expr"] for obj in objects(dashboard) if "expr" in obj)


def test_initial_slice_contains_exactly_two_stable_dashboards() -> None:
    loaded = dashboards()

    assert {dashboard["uid"] for dashboard in loaded} == {
        "ai1-openstack-overview",
        "ai1-telemetry-integrity",
    }
    assert {dashboard["title"] for dashboard in loaded} == {
        "AI1 / OpenStack — Overview",
        "AI1 — Telemetry Integrity and Cost",
    }


def test_dashboards_share_the_bounded_query_contract() -> None:
    for dashboard in dashboards():
        variables = dashboard["templating"]["list"]
        assert [variable["name"] for variable in variables] == [
            "DS_PROMETHEUS",
            "tenant_id",
            "infra",
        ]
        assert variables[0]["current"]["value"] == "thanos"
        assert variables[1]["query"] == "lab"
        assert variables[1]["multi"] is False
        assert variables[1]["includeAll"] is False
        assert variables[2]["query"] == "ai1"
        assert variables[2]["multi"] is False
        assert variables[2]["includeAll"] is False
        assert dashboard["time"] == {"from": "now-6h", "to": "now"}
        assert dashboard["refresh"] == "1m"

        target_objects = [obj for obj in objects(dashboard) if "expr" in obj]
        assert target_objects
        for target in target_objects:
            expression = target["expr"]
            assert 'tenant_id="$tenant_id"' in expression
            assert 'infra="$infra"' in expression
            assert 'infra_role="cloud-host"' in expression
            assert 'cloud="openstack"' in expression

        for obj in objects(dashboard):
            datasource = obj.get("datasource")
            if isinstance(datasource, dict):
                assert datasource["uid"] == "${DS_PROMETHEUS}"
            assert "repeat" not in obj


def test_dashboards_carry_terse_operator_guidance_and_relative_links() -> None:
    notes = []
    for dashboard in dashboards():
        note = next(panel for panel in dashboard["panels"] if panel["title"] == "Operator note")
        content = note["options"]["content"]
        notes.append(content)
        assert len(content) < 700
        for link in dashboard["links"]:
            assert link["url"].startswith("/d/")

    assert "does not prove OpenStack API" in notes[0]
    assert "Missing data is unknown, not zero" in notes[0]
    assert "an isolated sender cannot independently report its own outage" in notes[1]


def test_expensive_cardinality_queries_use_a_slower_minimum_interval() -> None:
    telemetry = next(
        dashboard for dashboard in dashboards() if dashboard["uid"] == "ai1-telemetry-integrity"
    )
    expensive = {
        "Visible active series",
        "Top metric families",
        "Top device labels",
        "dm-* devices",
        "dm-* series baseline",
    }

    for panel in telemetry["panels"]:
        if panel["title"] in expensive:
            assert {target["interval"] for target in panel["targets"]} == {"5m"}


def test_overview_covers_the_accepted_host_and_bounded_dataplane_signals() -> None:
    overview = next(
        dashboard for dashboard in dashboards() if dashboard["uid"] == "ai1-openstack-overview"
    )
    promql = expressions(overview)

    for metric in (
        "node_cpu_seconds_total",
        "node_memory_MemAvailable_bytes",
        "node_filesystem_avail_bytes",
        "node_pressure_cpu_waiting_seconds_total",
        "node_pressure_memory_waiting_seconds_total",
        "node_pressure_io_waiting_seconds_total",
        "cinder_thin_pool_data_percent",
        "cinder_thin_pool_metadata_percent",
        "node_systemd_unit_state",
        "node_network_receive_bytes_total",
        "node_network_transmit_errs_total",
        "ovs_interface_link_state",
        "ovs_datapath_lost_total",
        "ovs_datapath_missed_total",
        "ovs_datapath_flows",
    ):
        assert metric in promql
    assert 'device=~"eno1|eno2"' in promql


def test_telemetry_dashboard_uses_only_observed_sender_and_collector_metrics() -> None:
    telemetry = next(
        dashboard for dashboard in dashboards() if dashboard["uid"] == "ai1-telemetry-integrity"
    )
    promql = expressions(telemetry)

    for metric in (
        "prometheus_remote_storage_samples_failed_total",
        "prometheus_remote_storage_samples_dropped_total",
        "prometheus_remote_storage_queue_highest_sent_timestamp_seconds",
        "prometheus_remote_write_wal_storage_active_series",
        "openstack_telemetry_cinder_scrape_error",
        "openstack_telemetry_ovs_scrape_error",
    ):
        assert metric in promql
    for label in (
        "infra",
        "infra_role",
        "cloud",
        "cloud_region",
        "region",
        "stage",
        "lane",
        "tenant",
        "tenant_id",
    ):
        assert label in promql
    for forbidden_label in ("cluster", "cluster_role", "__replica__", "replica"):
        assert f'{forbidden_label}!=""' in promql


def test_chart_requires_an_explicit_known_nonempty_group_selection() -> None:
    schema = json.loads((CHART / "values.schema.json").read_text())
    ci_values = yaml.safe_load((CHART / "values-ci.yaml").read_text())

    assert schema["required"] == ["enabledGroups"]
    groups = schema["properties"]["enabledGroups"]
    assert groups["minItems"] == 1
    assert groups["uniqueItems"] is True
    assert "ai1-openstack" in groups["items"]["enum"]
    assert "ai1-openstack" in ci_values["enabledGroups"]


def test_template_uses_group_identity_and_folder_annotation() -> None:
    template = (CHART / "templates" / "configmap.yaml").read_text()

    assert 'printf "dashboards/%s/*.json" $group' in template
    assert 'printf "%s-%s" $group $base' in template
    assert 'grafana_dashboard: "1"' in template
    assert "annotations:\n    grafana_folder:" in template
    assert "OpenStack · ai1" in template
    assert "900 KiB dashboard ConfigMap safety limit" in template
