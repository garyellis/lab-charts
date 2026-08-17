# Alloy wrapper

This chart preserves the existing combined Grafana pipeline while offering a
portable, metrics-only Thanos Receive profile.

## Profile selection

Exactly one profile must be enabled:

- `profiles.default.enabled=true` is the backward-compatible default. Existing
  consumers that do not set any `profiles` values render exactly as before.
- `profiles.thanos.enabled=true` renders `<release>-thanos-profile`. Set
  `profiles.default.enabled=false`, then set
  `alloy.alloy.configMap.create=false` and
  `alloy.alloy.configMap.name=<release>-thanos-profile`.
- `profiles.custom.enabled=true` permits both built-ins to be disabled. It
  requires `profiles.custom.configMapName` and requires the upstream Alloy
  subchart to reference that same existing ConfigMap.

Enabling more than one profile, enabling none, selecting Thanos without a
valid HTTPS receiver and all seven external labels, or selecting custom without
a real ConfigMap reference fails `helm template`. Profile selection never
merges pipelines, so there is no accidental duplicate scrape or remote write.

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
