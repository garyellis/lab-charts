# lab-charts

Helm wrapper charts under `charts/`, a Python CLI (`chart-manager`) that
renders, validates, and tests them on local Kubernetes clusters, and a CI
pipeline that runs the same commands on pull requests.

## Prerequisites

- macOS or Linux, git.
- A container runtime — Docker Desktop, Colima, or OrbStack — running before
  any kind task.
- [`mise`](https://mise.jdx.dev). It installs and pins `helm`, `kubectl`,
  `kind`, `kubeconform`, `kyverno`, `uv`, and Python.

## Quickstart

```bash
git clone <repo> lab-charts
cd lab-charts
mise trust && mise install
mise run setup
uv run chart-manager doctor
uv run chart-manager chart validate grafana --env dev
```

`doctor` checks that the binaries, kubecontext, container runtime, and
backends chart-manager uses are usable. The last command renders `grafana` for `dev`, validates the
manifests against the Kubernetes schema, and runs the policies declared in
the chart's `chart-lifecycle.yaml`.

## Daily commands

| Command | What it does |
| --- | --- |
| `uv run chart-manager doctor` | Check tool, kubecontext, and backend prerequisites. |
| `uv run chart-manager chart validate <name> --env <env>` | Render one chart for one environment, then run its validators. `--all` validates every environment; with no chart named, the worklist comes from `git diff` against `origin/main`. |
| `mise run validate -- --all` | Validate every chart and environment in the repo. |
| `mise run schemas` | Verify or cache pinned upstream schema repositories. Chart changes only need validate; use `--update` to move upstream pins. |
| `uv run chart-manager chart test <name> --profile minimal` | Install the chart on a local kind cluster and run its Helm test hooks. |
| `uv run chart-manager chart test <name> --skip-requires` | On an existing cluster, verify bootstrap and required releases without upgrading them, then reinstall and test only the selected target. On a new cluster, install prerequisites but Helm-test only the selected target. |
| `uv run chart-manager local up --chart <name>` | Create or start the local cluster, run bootstrap releases, converge the chart. `--stack <name>` converges a `LocalStack` instead. |
| `uv run chart-manager local status` | Report cluster existence, releases, URLs, and host-port drift. |
| `uv run chart-manager local down` | Stop the cluster, preserving releases, data, and image caches. |
| `uv run chart-manager local reset --chart <name>` | Destroy and recreate the cluster, then converge. Required after changing creation-time kind settings. |
| `uv run chart-manager chart list` | List charts with lifecycle capability status. Same as `mise run charts`. |
| `uv run chart-manager chart show <name>` | Print one chart's normalized `ChartLifecycle` intent. |
| `uv run chart-manager plan --changed-file <path>` | Show the validation, chart-test and publish work a change selects, with reasons. |
| `uv run chart-manager chart publish <name>... --repository oci://harbor.local/charts` | Package and push charts to an OCI registry in one batch. |
| `uv run chart-manager chart upgrade charts/<name>` | Run Renovate in isolation and open an idempotent chart-upgrade PR. |
| `uv run chart-manager promote pr\|monitor\|test` | Operate on Flux HelmRelease resources in a separate GitOps repo. |
| `uv run chart-manager event list [chart[@version]]` | List lifecycle events, newest first. Events are off unless `EVENTS_BACKEND=cosmos` is exported; create its database and container once with `mise run events:provision`. `event emit --dry-run` previews a document without a backend. |
| `uv run chart-manager grafana dashboard export <uid> --to <path>` | Export one dashboard from the kind Grafana as canonical JSON. `lint` checks committed dashboards. |
| `mise run test` | Run the Python unit tests. |

## Local clusters

Four authored kinds share the `chartmanager.io/v1alpha1` API under
[`src/chart_manager/api/v1alpha1/`](src/chart_manager/api/v1alpha1/):

- `ChartWorkspace` (`.chart-manager/workspace.yaml`) — the checkout-owned
  chart, local-cluster, render, and policy locations, plus repository-wide
  validation and chart-test fanout. Its validation policy pins the one
  Kubernetes release used by every chart in a validation run.
- `LocalCluster` (`.chart-manager/local-cluster.yaml`) — the kind config path
  and an ordered, fail-fast bootstrap sequence. Entries may be a local
  `ChartLifecycle` profile, a raw local chart, a pinned OCI chart, or an exact
  chart version from an HTTPS Helm repository.
- `ChartLifecycle` (`charts/<name>/chart-lifecycle.yaml`) — each chart's
  profiles, values, namespace, timeout, dependencies, and Helm test gate.
- `LocalStack` (`.chart-manager/stacks/<name>.yaml`) — composes lifecycle,
  pinned OCI, and exact-version HTTPS repository releases. Composition only;
  no templating or orchestration.

All `local` commands target the single `chart-manager` cluster by default,
avoiding duplicate kind clusters and host-port conflicts from the shared
`kind-config.yaml`. The workspace selects the repository's `LocalCluster`;
named stacks resolve from that file's sibling `stacks/` directory.

`kind-config.yaml` owns creation-time settings: Kubernetes version, topology,
and whether kind's default CNI is disabled. This repo installs Cilium via a
bootstrap release; that is configuration, not chart-manager behavior. After
editing creation-time settings, run `local reset` — `local up` cannot apply
them.

The bootstrap is ordinary authored YAML:

```yaml
apiVersion: chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster:
    config: kind-config.yaml
    hooks:
      preProvision: [./scripts/corporate-kind, pre]
      postProvision: [./scripts/corporate-kind, post]
  bootstrap:
    releases:
      - type: lifecycle
        chart: charts/cilium
        profile: minimal
        runtimeValues:
          cilium.k8sServiceHost: ${kind.controlPlaneHost}
          cilium.k8sServicePort: ${kind.controlPlanePort}
        readiness:
          nodesReady: true
          workloadsReady: {namespace: kube-system, timeout: 15m}
      - type: repo
        name: metrics-server
        repo: https://kubernetes-sigs.github.io/metrics-server/
        chart: metrics-server
        version: 3.12.2
        namespace: kube-system
        values: []
        timeout: 10m
```

Repository releases use Helm's per-command `--repo` and `--version` flags;
chart-manager does not add or manage entries in the user's Helm repository
configuration. Plain HTTP repositories, qualified chart names, and version
ranges are rejected when the resource is loaded.

Provisioning hooks are a deliberately small, trusted-code boundary: at most
one argv command may be authored for `preProvision` and one for
`postProvision`. They are not shell strings and chart-manager does not source
their output. A path executable must be a repository-relative file inside the
checkout; a bare executable is resolved through `PATH` when run. Hooks execute
synchronously as the current user, from the repository root, and fail the run
immediately. Review hook changes as carefully as application code, do not put
credentials in argv or committed files, and read secrets only from an existing
credential store or inherited environment.

Hooks run by default for `local up`, `local reset`, and `chart test` when that
command provisions a cluster. They default off when `CI` is conventionally
truthy (`1`, `true`, `yes`, or `on`, case-insensitive). The explicit
`--run-provision-hooks` / `--no-run-provision-hooks` flag wins over that
default. Dry runs validate and display local hooks but never execute them;
`chart test --no-ensure-cluster`, `local down`, and `local status` never run
hooks. Every eligible up runs them even when the Kind cluster already exists,
so scripts must be idempotent.

The child inherits normal proxy and trust variables such as `HTTP_PROXY`,
`HTTPS_PROXY`, `NO_PROXY`, and `SSL_CERT_FILE`. Chart-manager also supplies
`CHART_MANAGER_HOOK_PHASE`, `CHART_MANAGER_ROOT`,
`CHART_MANAGER_CLUSTER_NAME`, and `CHART_MANAGER_KIND_CONFIG`; the post hook
additionally receives `CHART_MANAGER_KUBE_CONTEXT` and
`CHART_MANAGER_PROVIDER_TYPE`. A corporate wrapper can switch on the phase:

```sh
#!/bin/sh
set -eu
case "$CHART_MANAGER_HOOK_PHASE" in
  preProvision) corporate-runtime-login --non-interactive ;;
  postProvision) kubectl --context "$CHART_MANAGER_KUBE_CONTEXT" apply -f company-ca.yaml ;;
esac
```

Keep vendor-specific runtime, proxy, CA, and registry setup in that wrapper.
Chart-manager manages Kind only: it does not start, stop, or destroy Colima,
Podman, Rancher Desktop, Docker Desktop, or another host runtime. For a
persistent non-default Docker endpoint, configure your Docker context or set
`CHART_MANAGER_DOCKER_HOST`; hooks inherit the parent environment, while Kind
commands receive that configured daemon address directly.

`local status` reports state without judging it: an absent cluster or a failed
release is the answer and still exits 0. Filter in the caller:
`chart-manager local status -o json | jq '.releases[] | select(.status!="deployed")'`.

## Output and dry runs

`-o`/`--output` always names a format — `table`, `json`, `yaml`, plus `md`,
`github`, or `all` where supported. The default `auto` prints a table at a
terminal and `json` when stdout is a pipe or CI log, so
`chart-manager chart list | jq` needs no flag. `-o` before the subcommand sets
the invocation default; the command's own `-o` wins.

`--dry-run` resolves the same plan the real run would execute and prints it
without touching anything. `local up`/`down`/`reset`, `chart test`,
`chart cache clean`, `chart publish`, `chart upgrade`, and
`promote pr` take it. On `chart test` and `chart cache clean` the
plan is the only document the command produces, so `-o` without `--dry-run`
is a usage error.

One exception to learn: `grafana dashboard export` writes its file to `--to`,
and that file is always canonical JSON regardless of `-o`.

## CI

CI runs the same commands you run locally.

```text
chart-manager (import contracts, check, integration tests) ─────────────┐
chart-contracts (chart source contracts, dashboard lint) ──────────────┤
prep ──┬── validate ───────────────────────────────────────────────────┤
       └── sandbox-test (matrix per chart) ────────────────────────────┴── publish
```

`prep` computes the changed files and derives the validate and sandbox
matrices from `chart-manager plan -o github` — there is no second fanout
heuristic in workflow YAML. `sandbox-test` runs one kind job per changed
chart, so unrelated charts never gate a PR. `publish` pushes every directly
changed chart with version `<Chart.yaml version>-pr.<pr>.<run>.g<sha>`, where
`<run>` is the workflow run number, so later builds of a PR sort higher.

The validate job skips repository rendering for changes limited to documentation,
tests, or Renovate configuration. Other paths (including chart inputs, Python
code, tool pins, and workflows), empty/unknown diffs, and explicit `all`/`list`
runs verify the whole repository. CI restores the upstream schema repositories
using the committed `.chart-manager/schemas.lock.yaml` hash and runs
`mise run schemas`. Synchronization checks or downloads two pinned Git snapshots:
the complete selected Kubernetes version directory and the complete CRD catalog.
It never renders charts. Git uses a shallow, blob-filtered fetch and sparse
checkout for Kubernetes; all selected files are materialized during sync.
Validation checks their bytes against the pinned Git tree offline before using
local file paths. Missing snapshots require sync; corruption requires removing
the named snapshot and syncing again.

Ordinary chart work only needs `chart-manager chart validate`: adding a chart,
resource, or catalog-backed kind does not change the lock or require another
sync. `chart-manager doctor` checks whether the pinned
snapshots are ready without writing to the cache. Use
`chart-manager schemas sync --update` to resolve moving upstream refs and write
the lock after successful hydration. The lock contains policy and two commit
pins, with no per-resource inventory. `--refresh` has been removed.
Read-only YAML parsing uses ruamel’s C extension, installed through the package
dependencies, while preserving the safe YAML 1.2 loader and round-trip editing.
Validation can still update Helm dependencies and retain rendered files with
`--keep`; schema resolution itself has no online fallback.

Generated CRD schemas are automatic, disposable build outputs in a separate
cache. With `generateFromCRDs` enabled, dependencies are materialized before
provider discovery, without parsing environment values or rendering charts.
Discovery scans chart sources and packaged dependencies for CRDs; dynamic
resource templates are conservatively treated as potential providers. Plain
nonprovider charts are skipped. Uncached providers render together in one batch,
and their schemas are cached immediately, including on a fresh checkout. Later
validations reuse results keyed by chart files (including untracked templates
and vendored dependencies), chart-manager Python code, and the Helm executable.
Potential providers with local file dependencies or an explicit Helm version
selector are conservatively rendered each time. Broken potential providers and
unavailable dependencies still fail preparation. Changed or removed
CRDs take effect on the next validate, without editing the upstream lock. CI
restores per-chart derived results separately, keyed by chart, implementation,
and tool inputs instead of the commit SHA. Before saving, CI drops entries that
were not used by the current preparation and excludes merged schema generations.
It also caches materialized Helm dependencies, restoring only files absent from
the checkout when that chart's metadata and lock still match. Tracked vendored
archives are never replaced. Upstream snapshots are saved immediately after a
successful sync, so a later chart failure does not discard those downloads.
INFO logs report upstream verification, CRD cache hits and renders, and
validation totals. The final timing
separates preparation from execution; detailed runner logs are DEBUG-only.

Each generated group/version/kind must have one identical schema across all
charts and environments. Conflicting definitions fail with a SPEC error naming
their providers. Generated schemas take precedence over chart-local schemas,
which take precedence over upstream snapshots. Nullable CRD fields admit null
while retaining authored enum constraints.

Schema preparation failures include their actual diagnostics in the CI summary.
If upstream synchronization fails, CI runs render-only validation and uploads
retained manifests; this diagnostic fallback never turns the failure green.
Template rejection is a validation failure, dependency-fetch failure is an
environment failure, and process crashes remain tool failures.
The `chart-manager` job also runs every integration test (`mise run check:integration`);
a missing tool fails a test instead of skipping it.

The cache lives under `$XDG_CACHE_HOME/chart-manager/schemas/v3/`
(`~/.cache/chart-manager/schemas/v3/` when `XDG_CACHE_HOME` is unset or relative),
with separate `repositories/` and `derived/` directories.
`CHART_MANAGER_SCHEMA_CACHE_ROOT` (or `schema_cache_root:` in the operator config)
moves the root that holds `v3/`. Previous cache formats are left untouched and never
reused. Run `mise run schemas` once to populate the new upstream cache.
Automatic pruning of old snapshots and interrupted staging directories is
intentionally deferred to a future maintenance command.

`ignoreMissingSchemas` skips a kind only when no generated, chart-local, or
pinned upstream schema exists. The complete catalog is available even for kinds
not previously used in this workspace, so optional resources with catalog
schemas are now validated regardless of other charts' requirements.
Local schema templates accept `{{.Group}}`, `{{.ResourceKind}}`,
`{{.ResourceAPIVersion}}`, and `{{.KindSuffix}}`. Authored locations must end in
`.json` and contain `{{.ResourceKind}}`: literal files are rejected because
kubeconform would apply them to every resource type, including core resources.
Prefer `schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json` to keep
groups and versions distinct.

Unsupported template variables are configuration errors. Rejecting literal
schema locations is a breaking v1alpha1 validation change; migrate them to
kind-specific file templates before using this release.

Repositories without workspace schema policy must supply local schema locations
in each chart's `spec.validation.schemaLocations` to enable schema validation.
Alternatively, configure `.chart-manager/workspace.yaml` and initialize the lock
with `chart-manager schemas sync --update`. No implicit online schema fallback exists.

Breaking v1alpha1 change: chart-local `spec.validation.kubernetesVersion` has been
removed and is rejected. Move it to `spec.validation.kubernetesVersion` in
`.chart-manager/workspace.yaml`; all managed charts use that shared version.
Renovate follows `kubernetes/kubernetes` GitHub releases, whose publication
timestamps support the 14-day delay before updating the schema policy.

Publishing needs `HARBOR_REGISTRY`, `HARBOR_USERNAME`, and optionally
`HARBOR_PROJECT` (default `charts`) in the runner environment, plus
`HARBOR_PASSWORD` as a GitHub secret.

### Reproducing a CI failure

- Download `rendered-manifests-<run_id>` (validate) or
  `sandbox-logs-<chart>-<profile>-<run_id>` (sandbox-test) from the run's
  Artifacts panel.
- Validate failure: run `mise run schemas`, then
  `uv run chart-manager chart validate <name> --env <env>`.
- Sandbox failure: `uv run chart-manager chart test <name> --profile minimal`.
- If it looks environmental, run `uv run chart-manager doctor`
  first — it names the missing binary or unreachable backend.

## Adding or editing a chart

Each managed chart owns one `charts/<name>/chart-lifecycle.yaml` with
`apiVersion: chartmanager.io/v1alpha1`, `kind: ChartLifecycle`.
`spec.validation` declares environments, composed values, triggers, and
policies; `spec.chartTest` declares install profiles and their Helm test
gates, plus `dependentTests` — chart/profile tests to rerun when this chart
changes. Either capability can be absent or disabled; `spec.enabled: false`
pauses both. See
[`tests/fixtures/charts/passing-app/chart-lifecycle.yaml`](tests/fixtures/charts/passing-app/chart-lifecycle.yaml)
for a minimal example.

The accepted shape of that file is
[`chart_lifecycle.py`](src/chart_manager/api/v1alpha1/chart_lifecycle.py).
Local roots and their shared release types live beside it. Reading a kind
module and its shared vocabulary is reading the whole contract. See
[`docs/architecture.md`](docs/architecture.md) for why the contract lives
apart from the code that interprets it.

Changed files map to environments through the `triggers:` globs. Intentional
exclusions go in `triggerIgnores:`; files matching neither are reported as
coverage gaps, and `unmatchedChanges: all-environments` fans them out to every
environment instead.

## Repository workspace

`.chart-manager/workspace.yaml` is the versioned repository contract. Its
`chartsDir`, `localCluster`, `renderDir`, and `policiesDir` values are
repository-relative, use `/`, and cannot escape the checkout. `chartsDir`
alone may be `.`. The two `fanout` lists select the complete validation or
chart-test matrix when an additional shared input changes; they do not
publish, deploy, or mutate chart dependencies.

Policy changes automatically fan out validation. The workspace marker,
selected `LocalCluster`, its kind config and repository bootstrap charts, and
`chartTest.sharedCharts` automatically fan out chart tests. A
plain fanout path matches itself and descendants, `*` matches within one path
segment, and `**` matches zero or more segments.

Repository-bound commands find the nearest ancestor containing the fixed
workspace marker, so they behave the same from the checkout root or a nested
chart directory. `CHART_MANAGER_ROOT` (or `root:` in the operator config)
remains the machine-specific override; it must point at the directory that
holds the marker, since an explicit root is never walked up. There is no CLI
`--root` option. The workspace is required: with no marker, a
repository-bound command exits `5` and says to run from a chart repository
checkout or set `CHART_MANAGER_ROOT`. `version`, `event`, `promote`,
`grafana dashboard export`, and `grafana dashboard lint --path` work anywhere.
`doctor` runs anywhere: without a workspace it skips the schema checks and
says why; an invalid `workspace.yaml` exits `3`.

Layout lives only in the workspace. An unknown key in
`.chart-manager/config.yaml`, such as `charts_dir`, exits `3`. That config
file is read relative to the working directory, not the discovered workspace,
so from a nested directory pass `--config` explicitly.

Logs go to stderr; stdout stays safe for JSON. `CHART_MANAGER_LOG_LEVEL`
(default `INFO`) and `CHART_MANAGER_LOG_FORMAT=json` control detail and
shape.

## Troubleshooting

Start with `uv run chart-manager doctor`. It is read-only and cluster-free,
reports a stopped runtime instead of hanging on it, and prints the fix beside
each failure. The exit code classifies the problem: `127` missing binary,
`5` broken environment (no kubecontext, unreachable backend), `4` installed
but broken tool, `3` invalid configuration.

- kind nodes `NotReady` — expected until Cilium installs as the CNI.
- `kind: command not found` or cluster creation hangs — start the configured
  container runtime and verify the active Docker context or
  `CHART_MANAGER_DOCKER_HOST`.
- `mise: command not found` — install mise, then `mise trust` in the repo.
- A local URL stopped resolving after editing `kind-config.yaml` — creation
  settings need `local reset`, not `local up`; `local status` shows the
  missing host ports.

## More

- [`docs/architecture.md`](docs/architecture.md) — where a type belongs:
  the package layout and what stays out of the authored API contract.
- [`docs/chart-lifecycle-spec.md`](docs/chart-lifecycle-spec.md) — the
  `ChartLifecycle` resource and the plan/execute model behind the commands.
- [`docs/renovate-upgrades.md`](docs/renovate-upgrades.md) — how
  `chart upgrade` runs Renovate: auth, config layering, versioning, callback
  security.
