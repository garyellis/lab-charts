# Alloy wrapper

This chart preserves the existing combined Grafana pipeline while offering two
portable metrics-only profiles: the fleet-oriented Thanos profile and a
strict, minimal observability-hub collector.

## Profile selection

Exactly one profile must be enabled:

- `profiles.default.enabled=true` is the backward-compatible default. Existing
  consumers that do not set any `profiles` values render exactly as before.
- `profiles.thanos.enabled=true` renders `<release>-thanos-profile`. Set
  `profiles.default.enabled=false`, then set
  `alloy.alloy.configMap.create=false` and
  `alloy.alloy.configMap.name=<release>-thanos-profile`.
- `profiles.hubObservability.enabled=true` renders
  `<release>-hub-observability-profile`. Start with
  `values-hub-observability.yaml`, then supply the internal Receive URL,
  tenant, bounded external labels, and the release-derived ConfigMap name.
- `profiles.custom.enabled=true` permits both built-ins to be disabled. It
  requires `profiles.custom.configMapName` and requires the upstream Alloy
  subchart to reference that same existing ConfigMap.

Enabling more than one profile, enabling none, selecting Thanos without a
valid HTTPS receiver and all seven external labels, selecting hub observability
without its reviewed deployment/security contract, or selecting custom without
a real ConfigMap reference fails `helm template`. Profile selection never
merges pipelines, so there is no accidental duplicate scrape or remote write.

## Hub observability profile

The hub profile has one responsibility: scrape the health metrics needed to
observe the hub's Thanos, Thanos Ruler, Alertmanager, and Alloy components, then
write that bounded signal to an in-cluster Thanos Receive service.

Use the structural profile and an environment-owned values file together:

```yaml
# hub-values.yaml
profiles:
  hubObservability:
    receiver:
      url: http://thanos-receive.observability.svc:19291/api/v1/receive
      tenant: platform
    externalLabels:
      cluster: obs-w
      clusterRole: observability-hub
      lane: "01"
      stage: lab
      region: home
      cloud: openstack
      tenant: platform

alloy:
  alloy:
    configMap:
      name: alloy-hub-observability-profile
```

```bash
helm template alloy . \
  --namespace observability \
  -f values.yaml \
  -f values-hub-observability.yaml \
  -f hub-values.yaml
```

The release must be `alloy` for the ConfigMap name in the example. For another
release name, use `<release>-hub-observability-profile`.

The external-label API is deliberately limited to `cluster`, `cluster_role`,
`lane`, `stage`, `region`, `cloud`, and `tenant`. The Receive header tenant is
a separate routing value. Thanos Receive owns and adds the downstream
`tenant_id` label from that header; producers must not add `tenant_id` or
duplicate `infra` labels. Query hub series with the post-Receive `tenant_id`
plus `cluster` and the component's bounded `job` label.

The deployment contract is intentionally narrow:

- one unprivileged Deployment; no host PID, host network, host mounts, extra
  ports, init containers, or host tolerations;
- Alloy requests `50m` CPU and `128Mi` memory, has a `384Mi` memory limit, and
  has no CPU limit;
- no logs, traces, profiles, kubelet, cAdvisor, control-plane,
  kube-state-metrics, PodMonitor, Probe, or annotation-wide discovery;
- only ServiceMonitors in the release namespace carrying
  `observability.garyellis.io/hub-health: "true"` are discovered;
- one positive metric-name allowlist admits the reviewed component health,
  rule evaluation, notification, Compactor, Receive, and remote-write families;
- RBAC is limited to read-only ServiceMonitor, Service, EndpointSlice,
  Endpoint, Pod, and Namespace discovery; and
- the Receive URL must resolve through an in-cluster `*.svc` HTTP address.

The approval label is necessary but not sufficient for a new ServiceMonitor.
Each selected endpoint must also declare reviewed `sampleLimit` and
`targetLimit` values; the pinned Alloy component API has no profile-wide
default for those guards. Review label cardinality before adding a metric
family to the allowlist.

The consuming GitOps repository owns network policy. It must permit Kubernetes
API discovery, scrapes from Alloy to the explicitly labeled Services, and
remote write to Thanos Receive while denying unrelated egress.

Run the offline contract and lifecycle validation with:

```bash
charts/alloy/tests/profile-contract.sh charts/alloy
uv run chart-manager chart validate alloy --env hub
```

For a local kind installation with the Prometheus Operator CRDs available:

```bash
uv run chart-manager chart test alloy --profile hub-observability
```

The Thanos profile is environment-neutral. Consumers provide the Receive URL,
TLS server name/CA path, tenant header value, and the labels `cluster`,
`clusterRole`, `lane`, `stage`, `region`, `cloud`, and `tenant`. Kubernetes
mounts, RBAC, scheduling, image digests, and any local component-CA refresh
sidecar remain explicit deployment values because they are security decisions
specific to the cluster.

The pinned Alloy 1.12.1 component API does not yet expose TTL mode on
`prometheus.relabel` or a default sample limit on the operator discovery
components. This profile therefore uses the bounded 100,000-entry relabel LRU
and leaves per-monitor sample limits to each ServiceMonitor/PodMonitor. A later
Alloy upgrade may move those guards into the profile after its own review.
