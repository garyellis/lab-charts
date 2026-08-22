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
  allowlists and default to `sampleLimit: 1000` and `targetLimit: 2`; the
  second target is only rollout headroom. Raising these bounded inputs requires
  baseline evidence and a chart contract change.
- `thanosRuler.queryEndpoint` and the optional Alertmanager endpoint are
  internal service URLs. The Alertmanager endpoint defaults to the operator's
  `alertmanager-operated` service. Ruler is pinned to the reviewed Thanos
  v0.42 line.
- `telemetry.expectedHubTargets` and `expectedAi1Targets` are separate exact,
  bounded producer inventories. An alert exposes only its reviewed name,
  component, and source; job and instance remain query matchers.
- `openstack.expectedFamilies` is an enum. Cinder is intentionally excluded:
  the chart alerts on the host-local Cinder collector and thin-pool metrics,
  not a disabled OpenStack exporter API family.
- capacity, freshness, lag, evaluation, and PSI thresholds are bounded typed
  inputs. Arbitrary PromQL and arbitrary rule metadata are not accepted.
- runbook and dashboard base URLs are environment inputs. Every alert supplies
  `severity`, `owner`, `service`, `component`, `scope`, `alert_family`,
  `summary`, `description`, `impact`, `runbook_url`, and `dashboard_url`.

## Receiver custody

External delivery is off by default and the explicit `null` receiver is the
only route. Enabling the generic webhook requires only a `SecretKeySelector`:

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
and independent dead-man service are intentionally deferred decisions.

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
- remote-write failed, dropped/out-of-order, lagging, or missing self-metrics;
- required OpenStack exporter family missing or failing;
- healthy libvirt exporter with empty correlated inventory;
- Cinder collector failure/staleness and thin-pool data/metadata pressure;
- CPU, memory, and I/O PSI saturation plus missing-PSI integrity alerts;
- Thanos Compactor halted or missing;
- Ruler absence, failed/slow evaluation, dropped alerts between Ruler and
  Alertmanager, Alertmanager absence, rejected config, and notification failures.

Missing-data branches are explicit where absence is actionable. Counter rules
use `increase()` so process restarts and counter resets do not manufacture a
negative or hide a positive delta.

## Validation

```bash
helm lint charts/observability-alerting -f charts/observability-alerting/values-ci.yaml
charts/observability-alerting/tests/render-contract.sh charts/observability-alerting
PROMTOOL=promtool charts/observability-alerting/tests/promtool-contract.sh charts/observability-alerting
uv run chart-manager chart validate observability-alerting --env ci
uv run chart-manager chart test observability-alerting --profile minimal
```

The Helm test suite mounts the rendered production groups into `promtool` and
tests the healthy, pending, firing, missing-data, and recovery lifecycle for an
exact telemetry target. Because all groups share the same rendered rule file,
`promtool` also parses every production expression. A second hook verifies the
operator-created Ruler and Alertmanager StatefulSets roll out.
