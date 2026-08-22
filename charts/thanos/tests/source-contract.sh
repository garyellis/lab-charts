#!/usr/bin/env bash
set -euo pipefail

# Verify the bounded, opt-in hub ServiceMonitor contract without a cluster.
chart_dir="${1:-charts/thanos}"
render_dir="$(mktemp -d)"
trap 'rm -rf "$render_dir"' EXIT

default_render="$render_dir/default.yaml"
hub_render="$render_dir/hub.yaml"

helm template thanos "$chart_dir" --namespace observability >"$default_render"
helm template thanos "$chart_dir" --namespace observability \
  -f "$chart_dir/values-hub-observability.yaml" >"$hub_render"

uv run --extra dev python - "$default_render" "$hub_render" <<'PY'
from pathlib import Path
import re
import sys

import yaml


def resources(render_path: str, kind: str) -> list[dict]:
    return [
        document
        for document in yaml.safe_load_all(Path(render_path).read_text())
        if document and document.get("kind") == kind
    ]


expected = {
    "thanos-query": {
        "component": "query",
        "job": "integrations/thanos-query",
        "required": {"http_request_duration_seconds_bucket"},
    },
    "thanos-receive": {
        "component": "receive",
        "job": "integrations/thanos-receive",
        "required": {
            "http_request_duration_seconds_bucket",
            "thanos_objstore_bucket_operation_failures_total",
            "thanos_objstore_bucket_operations_total",
            "thanos_receive_forward_requests_total",
            "thanos_receive_hashring_nodes",
            "thanos_receive_replications_total",
            "thanos_receive_request_duration_seconds_bucket",
        },
    },
    "thanos-storegateway": {
        "component": "storegateway",
        "job": "integrations/thanos-storegateway",
        "required": {
            "http_request_duration_seconds_bucket",
            "thanos_objstore_bucket_operation_failures_total",
            "thanos_objstore_bucket_operations_total",
        },
    },
    "thanos-compactor": {
        "component": "compactor",
        "job": "integrations/thanos-compactor",
        "required": {
            "http_request_duration_seconds_bucket",
            "thanos_compact_halted",
            "thanos_compact_last_successful_run_timestamp_seconds",
            "thanos_objstore_bucket_operation_failures_total",
            "thanos_objstore_bucket_operations_total",
        },
    },
}

default_monitors = {
    monitor["metadata"]["name"]: monitor
    for monitor in resources(sys.argv[1], "ServiceMonitor")
}
assert set(default_monitors) == set(expected), default_monitors.keys()
assert resources(sys.argv[1], "PrometheusRule"), "default rules must remain compatible"
for name, monitor in default_monitors.items():
    labels = monitor["metadata"].get("labels") or {}
    assert "observability.garyellis.io/hub-health" not in labels, name
    assert "sampleLimit" not in monitor["spec"], name
    assert "targetLimit" not in monitor["spec"], name
    assert "honorLabels" not in monitor["spec"]["endpoints"][0], name

hub_monitors = {
    monitor["metadata"]["name"]: monitor
    for monitor in resources(sys.argv[2], "ServiceMonitor")
}
assert set(hub_monitors) == set(expected), hub_monitors.keys()
assert not resources(sys.argv[2], "PrometheusRule"), (
    "hub allowlists must not silently starve dependency-owned rules"
)
hub_services = {
    service["metadata"]["name"]: service
    for service in resources(sys.argv[2], "Service")
}
for name, contract in expected.items():
    monitor = hub_monitors[name]
    metadata = monitor["metadata"]
    spec = monitor["spec"]
    endpoint = spec["endpoints"][0]

    assert metadata["namespace"] == "observability", name
    assert metadata["labels"]["observability.garyellis.io/hub-health"] == "true", name
    assert spec["sampleLimit"] == 1000, name
    assert spec["targetLimit"] == 2, name
    assert spec["namespaceSelector"] == {"matchNames": ["observability"]}, name
    assert spec["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "thanos",
        "app.kubernetes.io/instance": "thanos",
        "app.kubernetes.io/component": contract["component"],
    }, name
    assert endpoint["port"] == "http", name
    assert endpoint["path"] == "/metrics", name
    assert endpoint["interval"] == "30s", name
    assert endpoint["scrapeTimeout"] == "10s", name
    assert endpoint["honorLabels"] is False, name
    assert endpoint["relabelings"] == [
        {
            "action": "replace",
            "targetLabel": "job",
            "replacement": contract["job"],
        },
        {
            "action": "replace",
            "targetLabel": "instance",
            "replacement": name,
        },
    ], name
    assert all(
        "pod" not in source.lower() and "address" not in source.lower()
        for relabeling in endpoint["relabelings"]
        for source in relabeling.get("sourceLabels", [])
    ), name

    selected_http_services = []
    for service in hub_services.values():
        service_labels = service["metadata"].get("labels", {})
        if all(
            service_labels.get(key) == value
            for key, value in spec["selector"]["matchLabels"].items()
        ) and any(port.get("name") == "http" for port in service["spec"]["ports"]):
            selected_http_services.append(service["metadata"]["name"])
    assert selected_http_services == [name], (name, selected_http_services)

    metric_relabelings = endpoint["metricRelabelings"]
    assert len(metric_relabelings) == 1, name
    allowlist = metric_relabelings[0]
    assert allowlist["action"] == "keep", name
    assert allowlist["sourceLabels"] == ["__name__"], name
    for family in {
        "up",
        "go_goroutines",
        "process_cpu_seconds_total",
        "process_resident_memory_bytes",
        *contract["required"],
    }:
        assert re.fullmatch(allowlist["regex"], family), (name, family)
    for excluded in (
        "container_cpu_usage_seconds_total",
        "kube_pod_info",
        "thanos_rule_evaluations_total",
        "alertmanager_notifications_total",
        "unreviewed_exporter_metric_total",
    ):
        assert not re.fullmatch(allowlist["regex"], excluded), (name, excluded)

all_jobs = {
    relabeling["replacement"]
    for monitor in hub_monitors.values()
    for relabeling in monitor["spec"]["endpoints"][0]["relabelings"]
    if relabeling["targetLabel"] == "job"
}
assert all("ruler" not in job and "alertmanager" not in job for job in all_jobs)
PY

hub_values=(-f "$chart_dir/values-hub-observability.yaml")

expect_hub_failure() {
  local description="$1"
  shift
  if helm template thanos "$chart_dir" --namespace observability \
    "${hub_values[@]}" "$@" >/dev/null 2>&1; then
    printf 'hub ServiceMonitor profile accepted %s\n' "$description" >&2
    exit 1
  fi
}

# The profile must replace, never overlap, the dependency-owned monitors.
if helm template thanos "$chart_dir" --namespace observability \
  --set hubServiceMonitors.enabled=true >/dev/null 2>&1; then
  echo "hub ServiceMonitor profile accepted the upstream monitors" >&2
  exit 1
fi
expect_hub_failure "global upstream monitors" \
  --set thanos.global.serviceMonitor.enabled=true
expect_hub_failure "a component upstream monitor" \
  --set thanos.query.serviceMonitor.enabled=true
expect_hub_failure "dependency-owned PrometheusRules" \
  --set thanos.global.thanosRules.enabled=true

# Only the reviewed four-component topology can opt into the bounded contract.
expect_hub_failure "disabled Query" --set thanos.query.enabled=false
expect_hub_failure "split Receive" --set thanos.receive.mode=dual
expect_hub_failure "sharded Store Gateway" \
  --set thanos.storegateway.sharded.enabled=true
expect_hub_failure "autoscaled Query targets" --set thanos.query.autoscaling.enabled=true
expect_hub_failure "autoscaled Store Gateway targets" \
  --set thanos.storegateway.autoscaling.enabled=true
expect_hub_failure "multiple Compactors" --set thanos.compactor.replicaCount=2
expect_hub_failure "too many Receive targets" --set thanos.receive.replicaCount=3
expect_hub_failure "too many Store Gateway targets" \
  --set thanos.storegateway.replicaCount=3

# JSON Schema owns absolute bounds; render validation owns topology-relative bounds.
expect_hub_failure "sampleLimit below 100" --set hubServiceMonitors.sampleLimit=99
expect_hub_failure "sampleLimit above 3000" --set hubServiceMonitors.sampleLimit=3001
expect_hub_failure "targetLimit below the rolling Query target count" \
  --set hubServiceMonitors.targetLimit=1
expect_hub_failure "targetLimit above 5" --set hubServiceMonitors.targetLimit=6
expect_hub_failure "an unreviewed scrape interval" \
  --set hubServiceMonitors.interval=15s
expect_hub_failure "an unknown configuration key" \
  --set hubServiceMonitors.unbounded=true
expect_hub_failure "a reserved common instance label" \
  --set-string 'thanos.global.commonLabels.app\.kubernetes\.io/instance=unstable'
expect_hub_failure "a reserved Query component label" \
  --set-string 'thanos.query.service.labels.app\.kubernetes\.io/component=other'
