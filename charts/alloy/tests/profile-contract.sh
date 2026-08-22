#!/usr/bin/env bash
set -euo pipefail

chart_dir="${1:-charts/alloy}"

default_render="$(helm template alloy "$chart_dir" --namespace observability)"
grep -q 'name: alloy' <<<"$default_render"
if grep -q 'alloy-thanos-profile' <<<"$default_render"; then
  echo "default render unexpectedly contains the Thanos profile" >&2
  exit 1
fi

thanos_render="$(helm template alloy "$chart_dir" --namespace observability -f "$chart_dir/tests/values-thanos.yaml")"
grep -q 'name: alloy-thanos-profile' <<<"$thanos_render"
grep -q 'prometheus.remote_write "thanos"' <<<"$thanos_render"
grep -q 'replacement = "https"' <<<"$thanos_render"

hub_render_file="$(mktemp)"
trap 'rm -f "$hub_render_file"' EXIT
helm template alloy "$chart_dir" \
  --namespace observability \
  -f "$chart_dir/values-hub-observability.yaml" \
  -f "$chart_dir/tests/values-hub-observability.yaml" >"$hub_render_file"

python3 - "$hub_render_file" <<'PY'
from pathlib import Path
import sys

import yaml

documents = [
    document
    for document in yaml.safe_load_all(Path(sys.argv[1]).read_text())
    if document
]

def resources(kind: str) -> list[dict]:
    return [document for document in documents if document.get("kind") == kind]

deployments = resources("Deployment")
assert len(deployments) == 1, "hub profile must render exactly one Deployment"
assert not resources("DaemonSet"), "hub profile must not render a DaemonSet"
assert not resources("StatefulSet"), "hub profile must not render a StatefulSet"

pod_spec = deployments[0]["spec"]["template"]["spec"]
assert not pod_spec.get("hostPID", False)
assert not pod_spec.get("hostNetwork", False)
assert all("hostPath" not in volume for volume in pod_spec.get("volumes", []))

containers = {container["name"]: container for container in pod_spec["containers"]}
alloy = containers["alloy"]
assert alloy["resources"] == {
    "limits": {"memory": "384Mi"},
    "requests": {"cpu": "50m", "memory": "128Mi"},
}
for container in containers.values():
    security = container.get("securityContext", {})
    assert security.get("privileged") is not True
    assert security.get("allowPrivilegeEscalation") is not True

cluster_roles = resources("ClusterRole")
assert len(cluster_roles) == 1
assert cluster_roles[0]["rules"] == [
    {
        "apiGroups": [""],
        "resources": ["endpoints", "pods", "services"],
        "verbs": ["get", "list", "watch"],
    },
    {
        "apiGroups": ["discovery.k8s.io"],
        "resources": ["endpointslices"],
        "verbs": ["get", "list", "watch"],
    },
    {
        "apiGroups": ["monitoring.coreos.com"],
        "resources": ["servicemonitors"],
        "verbs": ["get", "list", "watch"],
    },
    {
        "apiGroups": [""],
        "resources": ["namespaces"],
        "verbs": ["get", "list", "watch"],
    },
]

service_monitors = resources("ServiceMonitor")
assert len(service_monitors) == 1
assert service_monitors[0]["metadata"]["labels"][
    "observability.garyellis.io/hub-health"
] == "true"
endpoint = service_monitors[0]["spec"]["endpoints"][0]
assert service_monitors[0]["spec"]["sampleLimit"] == 500
assert service_monitors[0]["spec"]["targetLimit"] == 1
assert endpoint["relabelings"] == [
    {
        "action": "replace",
        "targetLabel": "job",
        "replacement": "integrations/alloy",
    }
]

profile = next(
    document
    for document in resources("ConfigMap")
    if document["metadata"]["name"] == "alloy-hub-observability-profile"
)["data"]["config.alloy"]
assert 'prometheus.operator.servicemonitors "hub_health"' in profile
assert '"observability.garyellis.io/hub-health" = "true"' in profile
assert 'alloy_(build_info|component_controller_running_components' in profile
assert 'prometheus_remote_storage_(enqueue_retries_total' in profile
assert 'prometheus_remote_write_wal_(out_of_order_samples_total|storage_active_series)' in profile
assert 'action        = "keep"' in profile
assert "tenant_id" not in profile
assert "infra" not in profile
for forbidden in (
    "discovery.kubernetes",
    "loki.",
    "otelcol.",
    "prometheus.exporter.unix",
    "prometheus.operator.podmonitors",
    "prometheus.operator.probes",
    "pyroscope",
):
    assert forbidden not in profile, f"hub config unexpectedly includes {forbidden}"
PY

helm template alloy "$chart_dir" -f "$chart_dir/tests/values-custom.yaml" >/dev/null

if helm template alloy "$chart_dir" -f "$chart_dir/tests/values-both.yaml" >/dev/null 2>&1; then
  echo "both built-in profiles were accepted" >&2
  exit 1
fi
if helm template alloy "$chart_dir" -f "$chart_dir/tests/values-none.yaml" >/dev/null 2>&1; then
  echo "an empty profile selection was accepted" >&2
  exit 1
fi

hub_values=(
  -f "$chart_dir/values-hub-observability.yaml"
  -f "$chart_dir/tests/values-hub-observability.yaml"
)
if helm template alloy "$chart_dir" "${hub_values[@]}" \
  --set alloy.controller.type=daemonset >/dev/null 2>&1; then
  echo "the hub profile accepted a DaemonSet" >&2
  exit 1
fi
if helm template alloy "$chart_dir" "${hub_values[@]}" \
  --set profiles.hubObservability.receiver.url=https://external.example.test/api/v1/receive >/dev/null 2>&1; then
  echo "the hub profile accepted an external Receive endpoint" >&2
  exit 1
fi
if helm template alloy "$chart_dir" "${hub_values[@]}" \
  --set alloy.alloy.resources.limits.cpu=100m >/dev/null 2>&1; then
  echo "the hub profile accepted a CPU limit" >&2
  exit 1
fi
if helm template alloy "$chart_dir" "${hub_values[@]}" \
  --set profiles.hubObservability.externalLabels.tenantId=value >/dev/null 2>&1; then
  echo "the hub profile accepted producer-owned tenant_id" >&2
  exit 1
fi
