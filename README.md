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
uv run chart-manager doctor --for 'chart validate'
uv run chart-manager chart validate grafana --env dev
```

`doctor` checks that the binaries, kubecontext, container runtime, and
backends a command needs are usable; `--for` narrows it to one command's
prerequisites. The last command renders `grafana` for `dev`, validates the
manifests against the Kubernetes schema, and runs the policies declared in
the chart's `chart-lifecycle.yaml`.

## Daily commands

| Command | What it does |
| --- | --- |
| `uv run chart-manager doctor` | Check tool, kubecontext, and backend prerequisites. `--for '<command>'` narrows to one command. |
| `uv run chart-manager chart validate <name> --env <env>` | Render one chart for one environment, then run its validators. `--all` validates every environment; with no chart named, the worklist comes from `git diff` against `origin/main`. |
| `mise run validate -- --all` | Validate every chart and environment in the repo. |
| `uv run chart-manager chart test <name> --profile minimal` | Install the chart on a local kind cluster and run its Helm test hooks. |
| `uv run chart-manager local up --chart <name>` | Create or start the local cluster, run bootstrap releases, converge the chart. `--stack <name>` converges a `LocalStack` instead. |
| `uv run chart-manager local status` | Report cluster existence, releases, URLs, and host-port drift. |
| `uv run chart-manager local down` | Stop the cluster, preserving releases, data, and image caches. |
| `uv run chart-manager local reset --chart <name>` | Destroy and recreate the cluster, then converge. Required after changing creation-time kind settings. |
| `uv run chart-manager chart list` | List charts with lifecycle capability status. Same as `mise run charts`. |
| `uv run chart-manager chart show <name>` | Print one chart's normalized `ChartLifecycle` intent. |
| `uv run chart-manager plan --changed-file <path>` | Show the validation and cluster-test work a change selects, with reasons. |
| `uv run chart-manager chart publish <name>... --repository oci://harbor.local/charts` | Package and push charts to an OCI registry in one batch. |
| `uv run chart-manager chart upgrade --path charts/<name>` | Run Renovate in isolation and open an idempotent chart-upgrade PR. |
| `uv run chart-manager helmrelease promote\|monitor\|test` | Operate on Flux HelmRelease resources in a separate GitOps repo. |
| `uv run chart-manager event list [chart[@version]]` | List lifecycle events, newest first. Events are off unless `EVENTS_BACKEND=cosmos` is exported; `event emit --dry-run` previews a document without a backend. |
| `uv run chart-manager grafana dashboard export <uid> --to <path>` | Export one dashboard from the kind Grafana as canonical JSON. `lint` checks committed dashboards. |
| `mise run test` | Run the Python unit tests. |

## Local clusters

Three authored kinds, all defined in
[`src/chart_manager/api/local/v1alpha1.py`](src/chart_manager/api/local/v1alpha1.py):

- `LocalCluster` (`.chart-manager/local-cluster.yaml`) — the kind config path
  and an ordered, fail-fast bootstrap sequence. Entries may be a local
  `ChartLifecycle` profile, a raw local chart, or a version-pinned OCI chart.
- `ChartLifecycle` (`charts/<name>/chart-lifecycle.yaml`) — each chart's
  profiles, values, namespace, timeout, dependencies, and Helm test gate.
- `LocalStack` (`.chart-manager/stacks/<name>.yaml`) — composes lifecycle and
  pinned OCI releases. Composition only; no templating or orchestration.

All `local` commands target the single `chart-manager` cluster by default,
avoiding duplicate kind clusters and host-port conflicts from the shared
`kind-config.yaml`. Set `CHART_MANAGER_LOCAL_CONFIG` to another
repository-relative `LocalCluster` file to use a different environment; named
stacks resolve from that file's sibling `stacks/` directory.

`kind-config.yaml` owns creation-time settings: Kubernetes version, topology,
and whether kind's default CNI is disabled. This repo installs Cilium via a
bootstrap release; that is configuration, not chart-manager behavior. After
editing creation-time settings, run `local reset` — `local up` cannot apply
them.

The bootstrap is ordinary authored YAML:

```yaml
apiVersion: local.chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster: {config: kind-config.yaml}
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
```

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
`helmrelease promote` take it. On `chart test` and `chart cache clean` the
plan is the only document the command produces, so `-o` without `--dry-run`
is a usage error.

One exception to learn: `grafana dashboard export` writes its file to `--to`,
and that file is always canonical JSON regardless of `-o`.

## CI

CI runs the same commands you run locally.

```text
layering (import contract, lint, types, unit tests)
prep ──┬── validate ──────────────────────────────┐
       └── sandbox-test (matrix per chart) ───────┴── publish (one batch)
```

`prep` computes the changed files and derives the validate and sandbox
matrices from `chart-manager plan -o github` — there is no second fanout
heuristic in workflow YAML. `sandbox-test` runs one kind job per changed
chart, so unrelated charts never gate a PR. `publish` pushes every directly
changed chart with version `<Chart.yaml version>-pr.<pr>.g<sha>`.

Publishing needs `HARBOR_REGISTRY`, `HARBOR_USERNAME`, and optionally
`HARBOR_PROJECT` (default `charts`) in the runner environment, plus
`HARBOR_PASSWORD` as a GitHub secret.

### Reproducing a CI failure

- Download `rendered-manifests-<run_id>` (validate) or
  `sandbox-logs-<chart>-<profile>-<run_id>` (sandbox-test) from the run's
  Artifacts panel.
- Validate failure: `uv run chart-manager chart validate <name> --env <env>`.
- Sandbox failure: `uv run chart-manager chart test <name> --profile minimal`.
- If it looks environmental, run `uv run chart-manager doctor --for 'chart test'`
  first — it names the missing binary or unreachable backend.

## Adding or editing a chart

Each managed chart owns one `charts/<name>/chart-lifecycle.yaml` with
`apiVersion: lifecycle.chartmanager.io/v1alpha1`, `kind: ChartLifecycle`.
`spec.validation` declares environments, composed values, triggers, and
policies; `spec.clusterTest` declares install profiles and their Helm test
gates, plus `dependentTests` — chart/profile tests to rerun when this chart
changes. Either capability can be absent or disabled; `spec.enabled: false`
pauses both. See
[`tests/fixtures/charts/passing-app/chart-lifecycle.yaml`](tests/fixtures/charts/passing-app/chart-lifecycle.yaml)
for a minimal example.

The accepted shape of that file is
[`src/chart_manager/api/lifecycle/v1alpha1.py`](src/chart_manager/api/lifecycle/v1alpha1.py);
local resources are
[`src/chart_manager/api/local/v1alpha1.py`](src/chart_manager/api/local/v1alpha1.py).
Reading one module is reading the whole contract. See
[`docs/architecture.md`](docs/architecture.md) for why the contract lives
apart from the code that interprets it.

Changed files map to environments through the `triggers:` globs. Intentional
exclusions go in `triggerIgnores:`; files matching neither are reported as
coverage gaps, and `unmatchedChanges: all-environments` fans them out to every
environment instead.

`charts/` is a default, not a fixed layout: set `CHART_MANAGER_CHARTS_DIR` to
a repository-relative path (absolute paths and `..` are rejected) and every
subsystem — discovery, change classification, validation, cluster services,
upgrades, dashboards — follows it.

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
- `kind: command not found` or cluster creation hangs — start Docker
  Desktop/Colima/OrbStack first.
- `mise: command not found` — install mise, then `mise trust` in the repo.
- A local URL stopped resolving after editing `kind-config.yaml` — creation
  settings need `local reset`, not `local up`; `local status` shows the
  missing host ports.

## More

- [`docs/architecture.md`](docs/architecture.md) — where a type belongs:
  authored API contract vs domain, services, and execution.
- [`docs/chart-lifecycle-spec.md`](docs/chart-lifecycle-spec.md) — the
  `ChartLifecycle` resource and the plan/execute model behind the commands.
- [`docs/renovate-upgrades.md`](docs/renovate-upgrades.md) — how
  `chart upgrade` runs Renovate: auth, config layering, versioning, callback
  security.
