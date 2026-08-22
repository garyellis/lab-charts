# observability-alerting

This chart owns the reusable evaluation and delivery control plane for platform
alerts. Prometheus Operator reconciles one `ThanosRuler`, one `Alertmanager`,
their selectors, an `AlertmanagerConfig`, rule groups, and component
`ServiceMonitor` resources. It requires the CRDs shipped by the repository's
`prometheus-operator` chart and is compatible with its pinned operator 0.87.1.

The chart does not own collectors, Thanos Query, network policy, dashboards,
runbooks, or Secrets. Those remain environment responsibilities. Every Thanos
rule group uses `partial_response_strategy: abort`; an incomplete query cannot
silently produce a healthy result.

The environment's network-policy owner must permit Ruler egress to Thanos
Query and `alertmanager-operated`, Alertmanager egress to the selected receiver,
and the hub collector to scrape `thanos-ruler-operated` on 10902 and
`alertmanager-operated` on 9093. The chart does not create a competing policy
owner.

## API boundary

- `identity.hub` selects post-Receive metrics with `tenant_id` + `cluster`;
  `identity.ai1` selects OpenStack-host metrics with `tenant_id` + `infra`.
  Ruler external labels use the cluster-style hub labels and deliberately omit
  reserved `tenant_id`, `job`, and `instance`. The schema rejects those fields.
  ServiceMonitors set `job` and leave identity to the producer plus Thanos
  Receive tenant enforcement. Both operator monitors retain exact self-metric
  allowlists and are fixed at `sampleLimit: 1000` and `targetLimit: 2`; the
  second target is only rollout headroom. Ruler and Alertmanager are singleton
  workloads in this phase and their PVCs are fixed at 5 Gi and 2 Gi. Changing
  that reviewed 7 Gi envelope requires a chart contract change.
- `thanosRuler.queryEndpoint` and the optional Alertmanager endpoint are
  restricted to exact in-cluster service URLs; hostnames that only resemble a
  `.svc` name and ports outside 1-65535 are rejected. The Alertmanager endpoint
  defaults to the operator's `alertmanager-operated` service. Ruler is pinned
  to the reviewed Thanos v0.42 line.
- `telemetry.expectedHubTargets` and `expectedAi1Targets` are separate exact,
  bounded producer inventories with unique names inside each inventory. Target
  names are limited to 46 characters so the derived `telemetry-target-...`
  incident key remains a valid 63-character Kubernetes label. An
  alert exposes only its reviewed name, component, and source; job and instance
  remain query matchers.
- `openstack.expectedFamilies` contains the seven reviewed exporter families:
  Identity, Glance, Nova, Neutron, Octavia (`loadbalancer`), Designate, and
  Placement. Cinder is intentionally excluded: the chart alerts on the
  host-local Cinder collector and thin-pool metrics, not a disabled OpenStack
  exporter API family.
- capacity, freshness, lag, evaluation, and PSI thresholds are bounded typed
  inputs. Arbitrary PromQL and arbitrary rule metadata are not accepted.
- runbook, dashboard, and Alertmanager external URLs are required environment
  inputs; the chart has no knowingly broken placeholder defaults. Every alert
  supplies `severity`, `owner`, `service`, `component`, `scope`,
  `alert_family`, `incident_key`, `summary`, `description`, `impact`,
  `runbook_url`, and `dashboard_url`. `obs-w` alerts also carry `cluster`;
  `ai1` alerts carry `infra`.

At minimum, an environment supplies its real link targets:

```yaml
links:
  runbookBaseUrl: https://runbooks.example.com/observability
  dashboardBaseUrl: https://grafana.example.com/d
alertmanager:
  externalUrl: https://alerts.example.com
```

## Receiver custody

External delivery is off by default and the explicit `null` receiver is the
only receiver. Critical and warning child routes group by `alertname`,
`severity`, and `scope`; critical notifications wait 15 seconds and repeat no
more often than hourly, while warnings repeat no more often than every four
hours. Resolved delivery remains enabled. A critical alert inhibits a warning
only when service, component, scope, family, incident key, cluster, and infra
all identify the same incident.

Enabling the generic webhook requires only a `SecretKeySelector`:

```yaml
delivery:
  enabled: true
  webhook:
    secretName: alertmanager-receiver
    secretKey: webhook-url
```

The referenced Secret must already exist in the release namespace. A GitOps
consumer can manage its ciphertext with SOPS/age; this chart never accepts a
URL, token, key, decrypted payload, or Secret manifest. The concrete receiver
and independent dead-man service are intentionally deferred decisions. Secret
names and data keys are schema-checked against Kubernetes identifier rules
before any resource is rendered.

`Watchdog` is disabled until that independent path exists. Enabling it without
an independently observed receiver does not prove end-to-end delivery.

## Ruler StoreAPI

The operator exposes Ruler results through
`thanos-ruler-operated.<namespace>.svc:10901` (`grpc`). The environment owner
must add
`dnssrv+_grpc._tcp.thanos-ruler-operated.<namespace>.svc.cluster.local` to
Thanos Query's StoreAPI endpoints. Ruler results live in its local 5 Gi TSDB;
this chart does not remote-write them through Receive. The Helm readiness test
asserts that the operator service exposes the `grpc` port at 10901.

## Alert portfolio

- exact target missing, down, or stale;
- remote-write failed, dropped/out-of-order, lagging, or missing required
  self-metric families;
- required OpenStack exporter family missing or failing;
- healthy libvirt exporter with empty correlated inventory;
- Cinder collector failure/staleness and thin-pool data/metadata pressure;
- CPU, memory, and I/O PSI saturation plus missing-PSI integrity alerts;
- Thanos Compactor halted, missing, or missing its halted-state metric family;
- Ruler absence, failed/warning/slow evaluation, dropped alerts between Ruler
  and Alertmanager, required Ruler self-metric families missing, Alertmanager
  absence, its config-reload family missing, rejected config, and notification
  failures.

Missing-data branches are explicit where absence is actionable. Counter rules
use `increase()` so process restarts and counter resets do not manufacture a
negative or hide a positive delta. Missing-family alerts are guarded by a
healthy component target, keeping component-down and telemetry-contract
incidents distinct. The reason-labelled
`prometheus_remote_storage_samples_dropped_total` CounterVec can be absent
until a drop occurs, so it is consumed opportunistically but is not required
without baseline evidence of a stable healthy-state child. Alertmanager
notification counters and histograms are likewise not required before a real
receiver initializes them; config reload is the startup-required family.
`ThanosRulerMissing` provides best-effort
dashboard coverage from retained telemetry; it is not independent detection of
an evaluator outage until the deferred dead-man receiver exists.

## Validation

```bash
helm lint charts/observability-alerting -f charts/observability-alerting/values-ci.yaml
charts/observability-alerting/tests/render-contract.sh charts/observability-alerting
PROMTOOL=promtool charts/observability-alerting/tests/promtool-contract.sh charts/observability-alerting
uv run chart-manager chart validate observability-alerting --env ci
uv run chart-manager chart test observability-alerting --profile minimal
```

The offline `promtool` suite parses all 56 rendered production rules and covers
healthy, pending, firing, missing-primary-data, overlap, counter-reset, and
recovery behavior across hub and AI1 target inventories. It separately proves
target absence, `up == 0`, stale timestamps, required-family absence/recovery,
and suppression of family alerts while the owning component is down. The render
contract also simulates critical/warning route selection and proves inhibition
cannot cross any bounded incident-identity field.

The Helm test verifies the operator-created Ruler and Alertmanager StatefulSets
and Ruler StoreAPI, then uses a test-only Query fixture to prove both scrape
targets are healthy, at least one rule group evaluated, evaluations produced no
failures or warnings, every required Ruler family exists before its healthy
value is asserted, Ruler queues and senders dropped no alerts, Alertmanager
accepted its generated configuration, and Alertmanager received the Watchdog.
Notification-specific family presence is not asserted before external delivery
is configured. The fixture is disabled by default and
derives its service address from the release name and namespace when enabled.
