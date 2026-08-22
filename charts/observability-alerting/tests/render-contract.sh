#!/usr/bin/env bash
set -euo pipefail

# This test owns the rendered alert, routing, inhibition, identity, and bounded
# resource contracts. Lifecycle validation separately checks Kubernetes schemas.
chart_dir="${1:-charts/observability-alerting}"
rendered_file="$(mktemp)"
rules_file="$(mktemp)"
delivery_file="$(mktemp)"
trap 'rm -f "${rendered_file}" "${rules_file}" "${delivery_file}"' EXIT

helm template observability-alerting "${chart_dir}" \
  --namespace observability \
  -f "${chart_dir}/values-ci.yaml" >"${rendered_file}"
helm template observability-alerting "${chart_dir}" \
  --namespace observability \
  -f "${chart_dir}/values-ci.yaml" \
  --set delivery.enabled=true \
  --set delivery.webhook.secretName=alertmanager-receiver \
  --set delivery.webhook.secretKey=webhook-url >"${delivery_file}"

yq -e 'select(.kind == "AlertmanagerConfig") | .spec.receivers[] | select(.name == "external-webhook") | .webhookConfigs[0].sendResolved == true' \
  "${delivery_file}" >/dev/null

for monitor in observability-alerting-ruler observability-alerting-alertmanager; do
  yq -e \
    "select(.kind == \"ServiceMonitor\" and .metadata.name == \"${monitor}\") | .spec.sampleLimit == 1000 and .spec.targetLimit == 2 and .spec.endpoints[0].interval == \"30s\" and .spec.endpoints[0].scrapeTimeout == \"10s\"" \
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

yq -e 'select(.kind == "ThanosRuler") | .spec.replicas == 1 and .spec.storage.volumeClaimTemplate.spec.resources.requests.storage == "5Gi" and .spec.queryEndpoints[0] == "http://observability-alerting-query-fixture.observability.svc:9090"' \
  "${rendered_file}" >/dev/null
yq -e 'select(.kind == "Alertmanager") | .spec.replicas == 1 and .spec.storage.volumeClaimTemplate.spec.resources.requests.storage == "2Gi"' \
  "${rendered_file}" >/dev/null

render_must_fail() {
  local failure="$1"
  shift
  if helm template observability-alerting "${chart_dir}" \
    --namespace observability \
    -f "${chart_dir}/values-ci.yaml" "$@" >/dev/null 2>&1; then
    echo "${failure}" >&2
    exit 1
  fi
}

render_must_fail "monitoring.sampleLimit outside the reviewed envelope was accepted" \
  --set monitoring.sampleLimit=1001
render_must_fail "monitoring.targetLimit outside the reviewed envelope was accepted" \
  --set monitoring.targetLimit=3
render_must_fail "a second Ruler replica was accepted in the singleton phase" \
  --set thanosRuler.replicas=2
render_must_fail "a second Alertmanager replica was accepted in the singleton phase" \
  --set alertmanager.replicas=2
render_must_fail "Ruler storage above the reviewed 5Gi envelope was accepted" \
  --set thanosRuler.storage.size=6Gi
render_must_fail "Alertmanager storage above the reviewed 2Gi envelope was accepted" \
  --set alertmanager.storage.size=3Gi
render_must_fail "Ruler memory above the reviewed resource envelope was accepted" \
  --set thanosRuler.resources.limits.memory=1Gi
render_must_fail "Alertmanager memory above the reviewed resource envelope was accepted" \
  --set alertmanager.resources.limits.memory=512Mi
render_must_fail "scrape timeout greater than or equal to interval was accepted" \
  --set monitoring.scrapeTimeout=30s
render_must_fail "an endpoint that only resembles in-cluster DNS was accepted" \
  --set tests.queryFixture.enabled=false \
  --set thanosRuler.queryEndpoint=http://thanos-query.observability.svc.evil:9090
render_must_fail "an Alertmanager endpoint that only resembles in-cluster DNS was accepted" \
  --set thanosRuler.alertmanagerEndpoint=http://alertmanager.observability.svc.evil:9093
render_must_fail "a sub-minute production alert duration was accepted" \
  --set alerts.for.telemetry=30s
render_must_fail "a knowingly broken runbook URL was accepted" \
  --set links.runbookBaseUrl=https://runbooks.example.invalid/observability
render_must_fail "a knowingly broken Alertmanager external URL was accepted" \
  --set alertmanager.externalUrl=http://alertmanager.example.invalid
render_must_fail "duplicate hub target names were accepted" \
  --set-json 'telemetry.expectedHubTargets=[{"name":"duplicate","job":"integrations/alloy","instance":"one:1234","component":"alloy"},{"name":"duplicate","job":"integrations/thanos","instance":"two:1234","component":"thanos"}]'
render_must_fail "duplicate AI1 target names were accepted" \
  --set-json 'telemetry.expectedAi1Targets=[{"name":"duplicate","job":"integrations/node_exporter","instance":"one:9100","component":"node-exporter"},{"name":"duplicate","job":"integrations/openstack_exporter","instance":"two:9180","component":"openstack-exporter"}]'

if yq -e 'select(.kind == "PrometheusRule") | .spec.groups[] | select(.partial_response_strategy != "abort")' \
  "${rendered_file}" >/dev/null 2>&1; then
  echo "a rule group permits a partial query response" >&2
  exit 1
fi
yq -o=json -I=0 'select(.kind == "PrometheusRule") | .spec.groups[].rules[]' \
  "${rendered_file}" >"${rules_file}"

rule_count=0
allowed_label_pattern='^(severity|owner|service|component|scope|alert_family|incident_key|cluster|infra|collector_family|expected_target|telemetry_source|pressure_type)$'
runbook_pattern='^https://runbooks[.]example[.]com/observability/(alerting-pipeline|cinder-capacity|host-saturation|libvirt-inventory-empty|openstack-collector-failure|remote-write-failure|telemetry-path-stale|thanos-compactor-halted)$'
dashboard_pattern='^https://grafana[.]example[.]com/d/(ai1-host-health|ai1-libvirt-hypervisor|ai1-openstack-cloud-services|ai1-openstack-overview|obs-w-alerting-health)$'

while IFS= read -r rule; do
  rule_count=$((rule_count + 1))
  alert_name="$(jq -r '.alert' <<<"${rule}")"

  if ! jq -e '
    . as $rule |
    ($rule.labels | type == "object") and
    (["severity", "owner", "service", "component", "scope", "alert_family", "incident_key"] | all(. as $key | ($rule.labels[$key] | type == "string" and length > 0))) and
    ($rule.labels.severity | IN("critical", "warning", "info")) and
    ($rule.labels.service | IN("telemetry", "openstack", "storage", "host", "thanos", "alerting")) and
    ($rule.labels.scope | IN("ai1", "obs-w", "fleet")) and
    ($rule.labels.incident_key | test("^[a-z0-9][a-z0-9-]{0,62}$"))
  ' <<<"${rule}" >/dev/null; then
    echo "${alert_name}: required bounded labels are invalid" >&2
    exit 1
  fi

  while IFS= read -r label; do
    if [[ ! "${label}" =~ ${allowed_label_pattern} ]]; then
      echo "${alert_name}: unreviewed label ${label}" >&2
      exit 1
    fi
  done < <(jq -r '.labels | keys[]' <<<"${rule}")

  if ! jq -e '
    . as $rule |
    (["summary", "description", "impact", "runbook_url", "dashboard_url"] | all(. as $key | ($rule.annotations[$key] | type == "string" and length > 0))) and
    ($rule.for | IN("5m", "10m", "15m", "30m", "1h") or ($rule.alert == "Watchdog" and $rule.for == "1m"))
  ' <<<"${rule}" >/dev/null; then
    echo "${alert_name}: annotations or pending duration violate the alert contract" >&2
    exit 1
  fi

  runbook_url="$(jq -r '.annotations.runbook_url' <<<"${rule}")"
  dashboard_url="$(jq -r '.annotations.dashboard_url' <<<"${rule}")"
  if [[ ! "${runbook_url}" =~ ${runbook_pattern} ]]; then
    echo "${alert_name}: unexpected runbook URL ${runbook_url}" >&2
    exit 1
  fi
  if [[ ! "${dashboard_url}" =~ ${dashboard_pattern} ]]; then
    echo "${alert_name}: unexpected dashboard URL ${dashboard_url}" >&2
    exit 1
  fi

  scope="$(jq -r '.labels.scope' <<<"${rule}")"
  expression="$(jq -r '.expr' <<<"${rule}")"
  case "${scope}" in
    obs-w)
      jq -e '.labels.cluster == "chart-manager" and (.labels | has("infra") | not)' <<<"${rule}" >/dev/null
      if [[ "${alert_name}" != "Watchdog" ]] &&
        { [[ "${expression}" != *'tenant_id="ci"'* ]] || [[ "${expression}" != *'cluster="chart-manager"'* ]] || [[ "${expression}" == *'infra="test"'* ]]; }; then
        echo "${alert_name}: obs-w expression lacks its exact tenant_id + cluster identity" >&2
        exit 1
      fi
      ;;
    ai1)
      jq -e '.labels.infra == "test" and (.labels | has("cluster") | not)' <<<"${rule}" >/dev/null
      if [[ "${expression}" != *'tenant_id="ci"'* ]] || [[ "${expression}" != *'infra="test"'* ]] || [[ "${expression}" == *'cluster="chart-manager"'* ]]; then
        echo "${alert_name}: ai1 expression lacks its exact tenant_id + infra identity" >&2
        exit 1
      fi
      ;;
    fleet)
      if [[ "${expression}" != *'tenant_id="ci"'* ]]; then
        echo "${alert_name}: fleet expression lacks tenant identity" >&2
        exit 1
      fi
      ;;
  esac
done <"${rules_file}"

if ((rule_count < 20)); then
  echo "expected a substantive alert portfolio, rendered only ${rule_count} rules" >&2
  exit 1
fi

yq -e 'select(.kind == "AlertmanagerConfig") | .spec.route.groupBy | join(",") == "alertname,severity,scope"' \
  "${rendered_file}" >/dev/null
yq -e 'select(.kind == "AlertmanagerConfig") | .spec.route.routes[] | select(.matchers[0].value == "critical") | select((.matchers | length) == 1) | select(.matchers[0].name == "severity") | select(.matchers[0].matchType == "=") | select(.groupWait == "15s") | select(.repeatInterval == "1h")' \
  "${rendered_file}" >/dev/null
yq -e 'select(.kind == "AlertmanagerConfig") | .spec.route.routes[] | select(.matchers[0].value == "warning") | select((.matchers | length) == 1) | select(.matchers[0].name == "severity") | select(.matchers[0].matchType == "=") | select(.repeatInterval == "4h")' \
  "${rendered_file}" >/dev/null

route_matches() {
  local severity="$1"
  local matches
  matches="$(SEVERITY="${severity}" yq -r '[select(.kind == "AlertmanagerConfig") | .spec.route.routes[] | select(.matchers[0].value == strenv(SEVERITY))] | length | select(. > 0)' "${rendered_file}")"
  [[ "${matches}" == "1" ]]
}

route_matches critical || { echo "critical route simulation did not select exactly one child" >&2; exit 1; }
route_matches warning || { echo "warning route simulation did not select exactly one child" >&2; exit 1; }
if route_matches info; then
  echo "an unreviewed info severity matched a child route" >&2
  exit 1
fi

expected_equal='alert_family cluster component incident_key infra scope service'
actual_equal="$(yq -r 'select(.kind == "AlertmanagerConfig") | .spec.inhibitRules[0].equal | sort | join(" ")' "${rendered_file}")"
if [[ "${actual_equal}" != "${expected_equal}" ]]; then
  echo "inhibition equality does not cover the complete bounded incident identity" >&2
  exit 1
fi
yq -e 'select(.kind == "AlertmanagerConfig") | .spec.inhibitRules[0] | select((.sourceMatch | length) == 1) | select(.sourceMatch[0].name == "severity") | select(.sourceMatch[0].matchType == "=") | select(.sourceMatch[0].value == "critical") | select((.targetMatch | length) == 1) | select(.targetMatch[0].name == "severity") | select(.targetMatch[0].matchType == "=") | select(.targetMatch[0].value == "warning")' \
  "${rendered_file}" >/dev/null

mutated_field=""

identity_value() {
  local side="$1"
  local field="$2"
  if [[ "${side}" == "target" && "${field}" == "${mutated_field}" ]]; then
    printf '%s' different
    return
  fi
  case "${field}" in
    service) printf '%s' storage ;;
    component) printf '%s' cinder ;;
    scope) printf '%s' ai1 ;;
    alert_family) printf '%s' capacity ;;
    incident_key) printf '%s' cinder-data ;;
    cluster) printf '%s' "" ;;
    infra) printf '%s' lab ;;
    *) return 1 ;;
  esac
}

is_inhibited() {
  local field
  for field in ${actual_equal}; do
    [[ "$(identity_value source "${field}")" == "$(identity_value target "${field}")" ]] || return 1
  done
}

is_inhibited || { echo "same-identity critical did not inhibit warning" >&2; exit 1; }
for identity_field in ${actual_equal}; do
  mutated_field="${identity_field}"
  if is_inhibited; then
    echo "inhibition crossed the ${identity_field} incident boundary" >&2
    exit 1
  fi
  mutated_field=""
done
