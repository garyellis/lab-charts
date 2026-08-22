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

uv run --extra dev python - "$hub_render_file" <<'PY'
from pathlib import Path
import re
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

assert not resources("ClusterRole"), "hub profile must not create cluster-wide RBAC"
assert not resources("ClusterRoleBinding"), "hub profile must not bind cluster-wide RBAC"
role = next(
    resource
    for resource in resources("Role")
    if resource["metadata"]["name"] == "alloy-hub-observability"
)
assert role["metadata"]["namespace"] == "observability"
assert role["rules"] == [
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
]
role_binding = next(
    resource
    for resource in resources("RoleBinding")
    if resource["metadata"]["name"] == "alloy-hub-observability"
)
assert role_binding["roleRef"] == {
    "apiGroup": "rbac.authorization.k8s.io",
    "kind": "Role",
    "name": "alloy-hub-observability",
}
assert role_binding["subjects"] == [{
    "kind": "ServiceAccount",
    "name": "alloy-hub-observability",
    "namespace": "observability",
}]
service_account = next(
    resource
    for resource in resources("ServiceAccount")
    if resource["metadata"]["name"] == "alloy-hub-observability"
)
assert service_account["automountServiceAccountToken"] is True
assert pod_spec["serviceAccountName"] == "alloy-hub-observability"
assert pod_spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"

service_monitors = resources("ServiceMonitor")
assert len(service_monitors) == 1
assert service_monitors[0]["metadata"]["labels"][
    "observability.garyellis.io/hub-health"
] == "true"
endpoint = service_monitors[0]["spec"]["endpoints"][0]
assert service_monitors[0]["spec"]["sampleLimit"] == 500
assert service_monitors[0]["spec"]["targetLimit"] == 1
assert endpoint["honorLabels"] is False
assert endpoint["relabelings"] == [
    {
        "action": "replace",
        "targetLabel": "job",
        "replacement": "integrations/alloy",
    }
]
metric_relabelings = endpoint["metricRelabelings"]
assert len(metric_relabelings) == 1
assert metric_relabelings[0]["action"] == "keep"
assert metric_relabelings[0]["sourceLabels"] == ["__name__"]

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
allowlist = metric_relabelings[0]["regex"]
for required_family in (
    "thanos_alert_queue_alerts_dropped_total",
    "alertmanager_notification_latency_seconds_bucket",
    "thanos_objstore_bucket_operation_failures_total",
    "thanos_objstore_bucket_operations_total",
    "http_request_duration_seconds_bucket",
):
    assert re.fullmatch(allowlist, required_family), required_family
assert 'action        = "keep"' in profile
assert allowlist in profile
assert 'action = "labeldrop"' in profile
assert 'regex  = "^(tenant_id|infra)$"' in profile
external_labels = profile.split("external_labels = {", 1)[1].split("}", 1)[0]
assert "tenant_id" not in external_labels
assert "infra" not in external_labels
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

expect_hub_failure() {
  local description="$1"
  shift
  if helm template alloy "$chart_dir" --namespace observability \
    "${hub_values[@]}" "$@" >/dev/null 2>&1; then
    echo "the hub profile accepted ${description}" >&2
    exit 1
  fi
}

# Both supported internal forms are exact: short .svc or the configured domain.
helm template alloy "$chart_dir" --namespace observability "${hub_values[@]}" \
  --set profiles.hubObservability.receiver.url=http://receiver.observability.svc.cluster.local:19291/api/v1/receive \
  >/dev/null

expect_hub_failure "a DaemonSet" --set alloy.controller.type=daemonset
expect_hub_failure "an external Receive endpoint" \
  --set profiles.hubObservability.receiver.url=https://external.example.test/api/v1/receive
expect_hub_failure "a deceptive .svc suffix" \
  --set profiles.hubObservability.receiver.url=http://receiver.observability.svc.evil.example/api/v1/receive
expect_hub_failure "a CPU limit" --set alloy.alloy.resources.limits.cpu=100m
expect_hub_failure "producer-owned tenant_id" \
  --set profiles.hubObservability.externalLabels.tenantId=value
expect_hub_failure "producer-owned infra" \
  --set profiles.hubObservability.externalLabels.infra=value
expect_hub_failure "dependency-owned cluster RBAC" --set alloy.rbac.create=true
expect_hub_failure "serviceAccount.create=false" --set alloy.serviceAccount.create=false
expect_hub_failure "an arbitrary service account" --set alloy.serviceAccount.name=arbitrary
expect_hub_failure "disabled service account token automount" \
  --set alloy.serviceAccount.automountServiceAccountToken=false
expect_hub_failure "a disabled Service" --set alloy.service.enabled=false
expect_hub_failure "a NodePort Service" --set alloy.service.type=NodePort
expect_hub_failure "an Unconfined pod seccomp profile" \
  --set alloy.global.podSecurityContext.seccompProfile.type=Unconfined
expect_hub_failure "an unset pod seccomp profile" \
  --set-string alloy.global.podSecurityContext.seccompProfile.type=
expect_hub_failure "a root Alloy container" --set alloy.alloy.securityContext.runAsUser=0
expect_hub_failure "a disabled config reloader" --set alloy.configReloader.enabled=false
expect_hub_failure "dependency-owned CRDs" --set alloy.crds.create=true
expect_hub_failure "a dependency network policy" --set alloy.networkPolicy.enabled=true
expect_hub_failure "arbitrary extra objects" --set-json \
  'alloy.extraObjects=[{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"unsafe"}}]'
