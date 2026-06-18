# helmrelease

the helmrelease service has two main focuses

# fluxcd pull requests
1. scan the fluxcd repository for files that contain helmrelease resources that match the chart and version. this could be more than one file of the same helm release or more than one chart or combination.
2. if the versions don't match, bump the version in a new pull request.
3. it should filter by dev/ folder initially

# watch the flux helm release status and helm test
1. detect updated helm release and confirm it succeeded and the helm test succeeded.
2. if either failed, take no action.. eventually we wll probably git revert pull request.
3. if both successful, check the subsequent folder test/ similar to the scap from fluxcd pull requests step 1 and 2.

## enabling helm test on a HelmRelease

Flux does not run Helm test by default. Add `spec.test.enable: true` to the
HelmRelease. When enabled, helm-controller runs the chart's `helm.sh/hook: test`
resources after a successful install or upgrade.

For the cert-manager wrapper chart, the test hook already exists at:

```text
charts/cert-manager/templates/tests/selfsign.yaml
```

That template creates a self-signed issuer/certificate and a test pod that waits
for the certificate to become Ready. To make Flux run it, use:

```yaml
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: cert-manager
  namespace: cert-manager
spec:
  interval: 10m
  chart:
    spec:
      chart: cert-manager
      version: "0.1.0"
      sourceRef:
        kind: HelmRepository
        name: lab-charts
        namespace: flux-system
      interval: 10m
  install:
    createNamespace: true
  targetNamespace: cert-manager
  test:
    enable: true
```

By default, a failing Helm test makes the HelmRelease fail. That is what this
promotion workflow wants: do not promote to the next environment unless the
HelmRelease reaches Ready and the test succeeded.

If a specific chart needs test failures to be informational only, Flux supports:

```yaml
spec:
  test:
    enable: true
    ignoreFailures: true
```

Do not use `ignoreFailures: true` for promotion gates.

## completion hooks / follow-up actions

HelmRelease does not have a generic "post-success callback" field. The practical
hook points are:

1. Helm chart hooks inside the chart:
   - `helm.sh/hook: test` for promotion gates.
   - `helm.sh/hook: post-install` or `helm.sh/hook: post-upgrade` for chart-owned
     Kubernetes Jobs that should run as part of the Helm action.
   - Use this only for work that belongs to the release itself. Promotion PRs
     should not be triggered from chart hooks.

2. HelmRelease status polling:
   - Watch `.status.conditions` for `Ready=True`.
   - Watch `Released=True` for install/upgrade success.
   - Watch `TestSuccess=True` with reason `TestSucceeded` when tests are enabled.
   - This is the simplest model for a CLI or scheduled watcher.

3. Flux notification-controller events:
   - Create an `Alert` for the HelmRelease.
   - Route successful events to a `Provider`, for example a generic webhook,
     Slack, Teams, DataDog, Sentry, Pub/Sub, etc.
   - This is better for audit trails and event-driven automation because the
     notification-controller observes state transitions as they happen.

Example generic webhook alert for successful cert-manager HelmRelease events:

```yaml
---
apiVersion: notification.toolkit.fluxcd.io/v1beta3
kind: Provider
metadata:
  name: promotion-webhook
  namespace: flux-system
spec:
  type: generic
  address: https://promotion-watcher.example.internal/flux-events
---
apiVersion: notification.toolkit.fluxcd.io/v1beta3
kind: Alert
metadata:
  name: cert-manager-promotion
  namespace: flux-system
spec:
  providerRef:
    name: promotion-webhook
  eventSeverity: info
  eventSources:
    - kind: HelmRelease
      name: cert-manager
      namespace: cert-manager
  inclusionList:
    - ".*succeeded.*"
  eventMetadata:
    chart: cert-manager
    source_env: dev
    promote_to: test
```

For this service, the preferred promotion flow is:

1. enable `spec.test.enable: true` on source-environment HelmReleases.
2. wait for `Ready=True` and `TestSuccess=True`.
3. create the next-environment promotion PR from the watcher/CLI.
4. record the decision in logs/audit output and, later, Flux events.

## CLI and FastAPI surfaces

The promotion logic should live in a shared service, not directly in either the
CLI or the HTTP handler. Both entry points should call the same core operation:

```text
PromotionService.promote(chart, source_env, target_env, version)
```

The CLI is useful for local/manual operations:

```bash
chart-manager helmrelease promote \
  --flux-repo git@github.com:org/fluxcd.git \
  --base main \
  --path dev/ \
  --chart-name cert-manager \
  --version 0.1.0 \
  --dry-run
```

The FastAPI surface is useful for event-driven promotion:

```text
Flux HelmRelease succeeds + helm test succeeds
  -> notification-controller sends webhook
  -> FastAPI validates the request
  -> FastAPI re-checks HelmRelease state
  -> shared promotion service creates or updates a PR
```

The webhook should be treated as a trigger, not as the source of truth. Before
opening a PR, the service should verify:

1. the HelmRelease name/namespace is allowed to promote.
2. the HelmRelease has `Ready=True`.
3. the HelmRelease has `TestSuccess=True` with reason `TestSucceeded`.
4. the observed chart version or digest is the version being promoted.
5. an equivalent promotion PR does not already exist.

## webhook authentication and cluster exposure

Do not expose the promotion API publicly. The default production shape should be:

1. run the API as an internal `ClusterIP` service.
2. apply a default-deny `NetworkPolicy`.
3. allow ingress only from the Flux `notification-controller` pod.
4. require an auth header or mTLS on the FastAPI endpoint.
5. re-check HelmRelease status before creating a PR.

NetworkPolicy limits who can reach the service. Application auth still matters
because NetworkPolicy can be misconfigured, labels can drift, and another
allowed pod could be compromised.

Example Flux `Provider` using an auth header from a Secret:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: promotion-webhook-auth
  namespace: flux-system
stringData:
  headers: |
    Authorization: Bearer replace-with-long-random-token
---
apiVersion: notification.toolkit.fluxcd.io/v1beta3
kind: Provider
metadata:
  name: promotion-webhook
  namespace: flux-system
spec:
  type: generic
  address: http://promotion-api.promotion-system.svc.cluster.local/webhooks/flux/helmrelease
  secretRef:
    name: promotion-webhook-auth
```

Example NetworkPolicy shape:

```yaml
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: promotion-api-ingress
  namespace: promotion-system
spec:
  podSelector:
    matchLabels:
      app: promotion-api
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: flux-system
          podSelector:
            matchLabels:
              app: notification-controller
      ports:
        - protocol: TCP
          port: 8000
```

Check the actual Flux pod labels before using this policy:

```bash
kubectl get pods -n flux-system --show-labels
```

Kubernetes RBAC does not authenticate normal HTTP requests between pods. RBAC
protects Kubernetes API calls. The promotion API needs its own HTTP auth or mTLS.

## notification-controller sidecar option

The promotion API can also run as a sidecar in the Flux `notification-controller`
pod. If the sidecar binds only to `127.0.0.1` and no Kubernetes Service is
created for it, only containers in that pod can reach it.

Provider address:

```yaml
apiVersion: notification.toolkit.fluxcd.io/v1beta3
kind: Provider
metadata:
  name: promotion-webhook
  namespace: flux-system
spec:
  type: generic
  address: http://127.0.0.1:8080/webhooks/flux/helmrelease
```

This is a reasonable prototype or lab shape because it makes the webhook private
to the Flux pod network namespace. It has tradeoffs:

1. lifecycle is coupled to `notification-controller`.
2. the sidecar may share the pod's ServiceAccount token.
3. compromise of the sidecar can have the same pod-level identity as Flux.
4. upgrades to Flux manifests may be more awkward.

If using the sidecar approach, harden it:

1. bind FastAPI to `127.0.0.1` only.
2. do not create a Service for the sidecar.
3. still require the Flux Provider auth header.
4. run as non-root.
5. use a read-only root filesystem.
6. avoid Kubernetes RBAC unless the API truly needs it.
7. mount GitHub credentials only if this process directly creates PRs.
8. make promotion operations idempotent.

Recommendation:

1. use sidecar + localhost for a first lab implementation.
2. use separate Deployment + ClusterIP + NetworkPolicy + auth/mTLS for a cleaner
   production deployment.

## v1 CLI: `chart-manager helmrelease promote`

The command takes a target version and opens one PR in a remote Flux GitOps
repo if any `HelmRelease` resources under `--path` drift from that version.

```bash
chart-manager helmrelease promote \
  --flux-repo git@github.com:org/lab-fluxcd.git \
  --path prod/ \
  --environment prod \
  --chart-name loki \
  --version 0.1.2
```

Behavior:

1. `--flux-repo` is an upstream git URL. The service shallow-clones it
   (`--depth 1 --branch <base-branch>`) into a temp directory and discards the
   workdir when the call ends. No local working tree assumed.
2. Scans `<workdir>/<path>` recursively for `kind: HelmRelease` documents
   whose `.spec.chart.spec.chart` equals `--chart-name`. Multi-doc YAML and
   nested subdirectories are supported. Matches against any
   `helm.toolkit.fluxcd.io/v2*` apiVersion.
3. For each match where `.spec.chart.spec.version` differs from `--version`,
   the file is rewritten with `ruamel.yaml` round-trip mode so comments, key
   order, and quote style are preserved. Multi-doc files with two HRs for the
   same chart are edited once.
4. Branch / PR title / PR body are deterministic
   (`promote/<env>/<chart>-<ver>`, `chore(<env>): promote <chart> to <ver>`),
   so re-running with the same inputs is idempotent at the git level.
5. Before mutating, the service calls `gh pr list --head <branch> --base
   <base>` — if a PR is already open for the branch, it returns that PR
   without re-editing or re-pushing.
6. If `gh pr create` fails after the push succeeded, the error includes the
   branch name so the operator can finish the step manually.
7. If nothing drifts, the command exits cleanly without opening a PR.
8. `--dry-run` performs the clone + scan and prints the planned branch / PR
   text without editing any files, running git, or calling `gh`.

Out of scope (handled by the orchestrator, not this command):

- *Which envs to fan out to for a chart publish.* The webhook handler /
  pipeline that triggers `promote` decides the (path, environment) per
  invocation; multi-env fan-out is N separate calls.
- *Gating on cluster state* (`Ready=True`, `TestSuccess=True`). Belongs in
  the trigger (e.g. a Harness stage that waits before firing the next call).
- *FastAPI surface.* Will reuse `PromoteService.promote()` unchanged.
