# Kyverno

Umbrella chart for [Kyverno](https://kyverno.io) v1.19.1 using upstream chart
`kyverno/kyverno` 3.9.1 from `https://kyverno.github.io/kyverno/`. Upstream
values stay nested under `kyverno:`.

This chart installs the policy engine and its CRDs only. The policies live in
[`kyverno-policies`](../kyverno-policies/README.md), a separate release: Helm
cannot create ClusterPolicies in the same release that installs their CRDs.

## Configuration decisions

- **Controllers.** Admission (webhooks) and reports (PolicyReports, including
  background scans of existing resources) run with one replica each. The
  background controller (generate and mutate-existing policies) and the
  cleanup controller (cleanup policies, `cleanup.kyverno.io/ttl`) are off
  because the lab has neither kind of policy. Enable them under
  `kyverno.backgroundController` / `kyverno.cleanupController` when it does.
- **Fail open.** `features.forceFailurePolicyIgnore` sets every resource
  webhook to `failurePolicy: Ignore`, so a crashed or unschedulable Kyverno
  cannot block Pod creation cluster-wide, including its own recovery. The cost
  is that Enforce policies are not enforced while Kyverno is down. Only
  webhooks for Kyverno's own CRDs (policies, exceptions) still fail closed.
- **Exclusions** are upstream's defaults: the webhooks skip `kube-system` and
  `kyverno`, and resourceFilters skip `kube-public`, `kube-node-lease`,
  Events, Nodes and similar. Background scans still report on `kube-system`.
- **PolicyExceptions** are enabled and accepted only from the `kyverno`
  namespace. An exception created elsewhere is admitted with a warning and
  ignored. See [adding an exception](../kyverno-policies/README.md#adding-an-exception).
- **Images** default to the chart appVersion, so the dependency version pins
  them. Upstream's own test image defaults to `:latest` and is pinned here.
- **Security contexts** are upstream's (non-root UID 65534, read-only root
  filesystem, all capabilities dropped, `RuntimeDefault` seccomp).
- **Metrics:** ServiceMonitors for the admission and reports controllers are
  off by default, so the chart does not depend on the prometheus-operator
  CRDs. Enable with `kyverno.admissionController.serviceMonitor.enabled: true`
  and `kyverno.reportsController.serviceMonitor.enabled: true`.

## Usage

```sh
helm dependency build charts/kyverno
helm upgrade --install kyverno charts/kyverno \
  --namespace kyverno --create-namespace \
  -f charts/kyverno/values.yaml
```

Uninstalling runs upstream's pre-delete hook, which scales Kyverno to zero and
removes its webhook configurations so none are left pointing at a missing
service.

### CRD ownership and uninstall order

This chart owns all of Kyverno's CRDs: they are rendered as ordinary templates
(`crds.install: true`), not files under `crds/`. Two consequences:

- **Upgrades cover the CRDs.** Because the CRDs are templated, `helm upgrade`
  reconciles them like any other object. The usual "Helm does not upgrade CRDs"
  caveat (which applies only to the `crds/` directory) does not apply here.
- **Uninstall cascade-deletes every policy.** `helm uninstall kyverno` deletes
  those CRDs, and Kubernetes then garbage-collects every custom resource of
  those kinds cluster-wide — including all ClusterPolicies and ValidatingPolicies
  owned by the separate [`kyverno-policies`](../kyverno-policies/README.md)
  release. This happens silently, and reinstalling this engine chart does **not**
  restore them; the `kyverno-policies` release must be reinstalled too.

  **Uninstall `kyverno-policies` before `kyverno`.**

## Validation

```sh
uv run chart-manager chart validate kyverno
uv run chart-manager local up --chart kyverno --profile minimal
helm test kyverno -n kyverno
```

The chart's helm test proves admission decides rather than just that the pod
is Ready. It installs a test-only ValidatingPolicy in Deny mode that matches
only the test's own Pods, then scales up two ReplicaSets in `default` that
differ only in a `verdict` label. The `allow` ReplicaSet creates its Pod; the
`deny` one reports `ReplicaFailure` from the Kyverno webhook. Upstream's tests
also run and check controller probes and metrics endpoints.

## Planned

Image signature verification (`verifyImages` / ImageValidatingPolicy) is a
follow-up and is not configured here.
