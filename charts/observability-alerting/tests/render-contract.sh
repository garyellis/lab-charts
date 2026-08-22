#!/usr/bin/env bash
set -euo pipefail

# This test owns the bounded ServiceMonitor API and its positive metric
# allowlists; lifecycle validation separately checks the full rendered schema.
chart_dir="${1:-charts/observability-alerting}"
rendered_file="$(mktemp)"
trap 'rm -f "${rendered_file}"' EXIT

helm template observability-alerting "${chart_dir}" \
  --namespace observability \
  -f "${chart_dir}/values-ci.yaml" >"${rendered_file}"

for monitor in observability-alerting-ruler observability-alerting-alertmanager; do
  yq -e \
    "select(.kind == \"ServiceMonitor\" and .metadata.name == \"${monitor}\") | .spec.sampleLimit == 1000 and .spec.targetLimit == 2" \
    "${rendered_file}" >/dev/null
done

ruler_allowlist='^(?:prometheus_rule_evaluation_failures_total|prometheus_rule_group_last_duration_seconds|prometheus_rule_group_last_evaluation_timestamp_seconds|thanos_alert_queue_alerts_dropped_total|thanos_alert_sender_alerts_dropped_total|thanos_rule_evaluation_with_warnings_total)$'
alertmanager_allowlist='^(?:alertmanager_config_last_reload_successful|alertmanager_notification_latency_seconds_bucket|alertmanager_notifications_failed_total|alertmanager_notifications_total)$'

yq -e \
  "select(.kind == \"ServiceMonitor\" and .metadata.name == \"observability-alerting-ruler\") | .spec.endpoints[0].metricRelabelings[] | select(.action == \"keep\" and (.sourceLabels | length) == 1 and .sourceLabels[0] == \"__name__\" and .regex == \"${ruler_allowlist}\")" \
  "${rendered_file}" >/dev/null

yq -e \
  "select(.kind == \"ServiceMonitor\" and .metadata.name == \"observability-alerting-alertmanager\") | .spec.endpoints[0].metricRelabelings[] | select(.action == \"keep\" and (.sourceLabels | length) == 1 and .sourceLabels[0] == \"__name__\" and .regex == \"${alertmanager_allowlist}\")" \
  "${rendered_file}" >/dev/null

if helm template observability-alerting "${chart_dir}" \
  --set monitoring.sampleLimit=3001 >/dev/null 2>&1; then
  echo "monitoring.sampleLimit above the reviewed ceiling was accepted" >&2
  exit 1
fi

if helm template observability-alerting "${chart_dir}" \
  --set monitoring.targetLimit=4 >/dev/null 2>&1; then
  echo "monitoring.targetLimit above the reviewed ceiling was accepted" >&2
  exit 1
fi
