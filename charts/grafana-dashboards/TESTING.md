# Testing

Run the dashboard and chart gates without contacting a cluster:

```bash
mise exec -- uv run chart-manager grafana dashboard lint -o table
mise exec -- helm lint charts/grafana-dashboards \
  -f charts/grafana-dashboards/values-ci.yaml
mise exec -- uv run chart-manager chart validate grafana-dashboards --env ci
```

Prove the production selection independently:

```bash
mise exec -- helm template grafana-dashboards \
  charts/grafana-dashboards \
  --namespace observability \
  --set 'enabledGroups={ai1-openstack}'
```

Acceptance requires exactly five ConfigMaps, group-qualified names, discovery
label `grafana_dashboard=1`, and folder annotation
`grafana_folder="OpenStack · ai1"`. The render must not contain dashboards from
`networking`, `obs-w-alerting`, `platform`, or `slo`.

Prove the alerting selection independently:

```bash
mise exec -- helm template grafana-dashboards \
  charts/grafana-dashboards \
  --namespace observability \
  --set 'enabledGroups={obs-w-alerting}'
```

Acceptance requires exactly one ConfigMap, dashboard UID
`obs-w-alerting-health`, datasource-variable default `thanos`, discovery label
`grafana_dashboard=1`, and folder annotation
`grafana_folder="Observability · Alerting"`. No other dashboard group may be
rendered. Before production reconciliation, verify the Ruler, Alertmanager,
Alloy, and Thanos job labels and dashboard metric names against saved target
metadata; an empty panel is unknown and must not be accepted as a healthy zero.

A render with no selection, an empty selection, a duplicate group, or an
unknown group must fail schema validation. Malformed JSON, missing UIDs,
oversized payloads, hard-coded datasource UIDs, duplicate UIDs, duplicate
rendered names, and unsupported URL schemes must fail their corresponding
offline gate.

After an authorized deployment, verify the sidecar can get/list/watch only
ConfigMaps in `observability`, Grafana lists all five stable UIDs under
`OpenStack · ai1` plus `obs-w-alerting-health` under
`Observability · Alerting` when that group is selected, and a Grafana restart
restores every selected dashboard from Git.
