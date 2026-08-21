# Grafana dashboards

This chart turns reviewed dashboard JSON into ConfigMaps for Grafana's
dashboard sidecar. Consumers must select an explicit bounded group; the chart
never installs every dashboard implicitly.

## Contract

```yaml
enabledGroups:
  - ai1-openstack
```

`enabledGroups` is required, non-empty, unique, and limited to directories
owned under `dashboards/`. An unknown group or a selected group with no JSON
fails rendering.

Each dashboard becomes one collision-resistant ConfigMap with:

- discovery label `grafana_dashboard: "1"`;
- group-derived `grafana_folder` annotation;
- group-qualified name with a stable path hash; and
- one unmodified JSON data entry.

The `ai1-openstack` group maps to the Grafana folder `OpenStack · ai1`.
Dashboard payloads above 900 KiB fail before approaching the ConfigMap limit.
Additional labels cannot replace chart-owned identity or discovery labels.

The group contains four deliberately separate operator views:

- `AI1 / OpenStack — Overview`: concise host and bounded-dataplane landing page;
- `AI1 — Host Health, Power and Thermals`: node-exporter CPU, memory, storage,
  network, systemd, hwmon, cooling, throttling, and RAPL component evidence;
- `AI1 / OpenStack — Cloud Services and Capacity`: independently managed
  openstack-exporter collection, agents, resources, quotas, and capacity; and
- `AI1 — Telemetry Integrity and Cost`: remote-write, freshness, identity, and
  cardinality evidence.

RAPL panels are explicitly CPU package/component telemetry, not whole-server
or wall electricity. The dashboards never add nested package and core energy
domains. A future external power meter or BMC exporter must remain a separately
named source.

## Authoring

Place dashboards at `dashboards/<group>/<name>.json`. Provisioned dashboards
remain editable in JSON so they can be imported for development; Grafana's
file provider must set `allowUiUpdates: false` and Git remains authoritative.

Use `${DS_PROMETHEUS}` for Prometheus-compatible data sources. The exporter
normalizes the live `thanos` and `mimir` UIDs:

```bash
uv run chart-manager grafana dashboard export <uid> \
  --to charts/grafana-dashboards/dashboards/<group>/<name>.json
uv run chart-manager grafana dashboard lint -o table
```

For `ai1-openstack`, every PromQL expression must scope both
`tenant_id="$tenant_id"` and `infra="$infra"`. Keep operator notes short:
state what the panel proves, how to interpret missing data, and the next
drilldown.

`tenant_id` is the Thanos receive tenant and must never be overloaded with an
OpenStack project. Cloud-resource panels use normalized
`openstack_project_id`, `openstack_project_name`, and `openstack_region` labels.
The two project variables are single-select, project-bounded queries; do not add
server, volume, port, address, device, or other unbounded resource variables.
Missing exporter or node series mean unknown, never healthy zero.

## Deployment

Flux should own the release. Pair the chart with a Grafana sidecar that watches
ConfigMaps in the release namespace, reads `grafana_folder` as an annotation,
and uses a read-only file provider with deletion enabled for pruning.

See [TESTING.md](TESTING.md) for offline gates. Cluster-backed tests are for an
authorized sandbox only; they are not required to validate the ConfigMap-only
render.
