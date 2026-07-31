# lab-charts

## What this repo is

A collection of Helm wrapper charts under `charts/`, a Python CLI (`chart-manager`) that renders, schema-checks, and tests them on local Kubernetes clusters, and a CI pipeline that runs the same commands. Everything you do locally is what CI does on a pull request.

## Prerequisites

- macOS or Linux.
- A container runtime — Docker Desktop, Colima, or OrbStack — **running** before any kind task.
- Git.
- [`mise`](https://mise.jdx.dev) — a polyglot tool version manager. It installs and pins `helm`, `kubectl`, `kind`, `kubeconform`, `kyverno`, `uv`, and Python for you.

## Quickstart

From clone to a green validate run on one chart in roughly thirty seconds:

```bash
git clone <repo> lab-charts
cd lab-charts
mise trust
mise install
mise run setup
uv run chart-manager validate chart --chart grafana --env dev
```

`mise install` pulls every pinned tool. `mise run setup` installs the Python CLI into a uv-managed venv. The final command renders the `grafana` chart for the `dev` environment, validates the manifests against the Kubernetes schema, and runs the policy set declared under `spec.validation` in its `chart-lifecycle.yaml`.

## Daily commands

| Command | What it does |
| --- | --- |
| `uv run chart-manager validate chart --chart <name> --env <env>` | Render one chart for one authored env, then run its enabled validators. |
| `uv run chart-manager validate chart --chart <name> --all` | Validate every environment authored for one chart. |
| `mise run validate -- --all` | Same as above, fanned out across every chart and every env declared in the repo. |
| `uv run chart-manager charts test --chart <name-or-path> --profile minimal` | Spin up a local Kubernetes test cluster, install the chart, and run its Helm test hooks when enabled. `--namespace` explicitly overrides the profile namespace. |
| `uv run chart-manager local up --chart <name-or-path> --profile minimal` | Create or start the chart's local cluster, run configured bootstrap releases, and converge the chart. |
| `uv run chart-manager local up --stack <name-or-path>` | Converge a `LocalStack` from `.chart-manager/stacks/<name>.yaml` or an explicit YAML path. |
| `uv run chart-manager local down` | Stop the configured local cluster while preserving releases, data, and image caches. |
| `uv run chart-manager local reset --chart <name-or-path>` | Destroy and recreate that chart's cluster, then converge it. Use `--stack` for a stack. |
| `mise run charts` | List every chart wrapper the CLI knows about. |
| `mise run test` | Run the Python unit tests for the CLI. |
| `uv run chart-manager ci impact --changed-file charts/<name>/values.yaml` | Explain validation and cluster-test fanout for explicit changed files. |
| `uv run chart-manager publish grafana loki --repository oci://harbor.local/charts --version-suffix pr.318.g1a2b3c4` | Prepare a batch, then publish it to an authenticated OCI registry. |
| `uv run chart-manager upgrade --path charts/<name>` | Discover Renovate updates in an isolated worktree and open an idempotent chart-upgrade PR. |

Local operation has three authored concepts:

- `LocalCluster` in `.chart-manager/local-cluster.yaml` owns the Kind config
  path and an ordered, fail-fast bootstrap sequence. Bootstrap entries may use
  a local `ChartLifecycle` profile, a raw local chart, or a version/digest-pinned
  OCI chart. Chart-manager does not select a CNI.
- `ChartLifecycle` owns each local chart's profile, values, namespace, timeout,
  dependencies, and Helm test gate.
- `LocalStack` optionally composes lifecycle releases and pinned OCI releases.
  It is intentionally narrower than Helmfile: composition only, with no
  templating or orchestration language.

All `local up`, `local down`, and `local reset` targets use the single
`chart-manager` cluster by default. This avoids duplicate Kind clusters and
host-port conflicts from the shared `kind-config.yaml`. Select another
Pydantic-configured `.chart-manager` directory when a different
`LocalCluster` and Kind configuration are needed.

The repository's `kind-config.yaml` controls its Kubernetes version, topology,
container runtime integration, and whether Kind's default CNI is disabled.
This repository chooses its Cilium wrapper in `LocalCluster`; another
repository can select a different networking chart or retain Kind's default
without any chart-manager code change. Run
`chart-manager local reset --chart <name-or-path>` (or `--stack`) after
changing creation-time Kind settings.

Set `CHART_MANAGER_LOCAL_CONFIG` to another repository-relative LocalCluster
file when `.chart-manager/local-cluster.yaml` is not the desired environment.
Named stacks are resolved from the selected file's sibling `stacks/` directory.

This repository's bootstrap is ordinary authored configuration:

```yaml
apiVersion: local.cmg.io/v1alpha1
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

A named stack is similarly small:

```yaml
apiVersion: local.cmg.io/v1alpha1
kind: LocalStack
metadata: {name: platform}
spec:
  releases:
    - {type: lifecycle, chart: charts/grafana, profile: minimal}
    - type: oci
      name: metrics-server
      chart: oci://registry.example/charts/metrics-server
      version: 1.2.3
      namespace: kube-system
      values: []
      timeout: 5m
```

Use `uv run chart-manager validate chart --help` for local chart validation and
`uv run chart-manager validate run --help` for changed-worklist and repository-wide runs.

## CI

CI uses the same **`chart-manager charts test`** execution path as
`mise run kind-test`. A `prep` job inspects the PR diff and decides which charts
changed; `validate` runs against the full set; `sandbox-test` fans out as a
matrix with one kind job per changed chart so unrelated charts never gate your
PR.

PR publishing uses each chart's `Chart.yaml` version plus a prerelease suffix
(`1.2.3-pr.<pr>.g<sha>`). The workflow logs in once and publishes all directly
changed charts in one batch. Configure `HARBOR_REGISTRY`, `HARBOR_USERNAME`,
and optionally `HARBOR_PROJECT` in the runner environment, plus
`HARBOR_PASSWORD` as a GitHub secret. `HARBOR_PROJECT` defaults to `charts`,
matching the local Harbor project and a portable ACR repository namespace; set
it explicitly when a registry uses a different prefix.

```text
prep ──┬── validate ──────────────────────────────┐
       └── sandbox-test (matrix per chart) ───────┴── publish (one batch)
```

Validation selection and the sandbox chart/profile matrix are derived by the
lifecycle impact service. The workflow consumes
`chart-manager ci cluster-test-matrix` instead of maintaining a second fanout
heuristic in YAML.

## Reproducing a CI failure

- Open the failed run and download `rendered-manifests-<run_id>` (validate) or `sandbox-logs-<chart>-<run_id>` (sandbox-test) from the Artifacts panel.
- Reproduce a validate failure locally with `uv run chart-manager validate chart --chart <name> --env <env>`.
- Reproduce a sandbox-test failure locally with `mise run kind-test -- --chart <name> --profile minimal`.

## Adding or editing a chart

Each managed chart owns one standalone
`charts/<name>/chart-lifecycle.yaml` resource with
`apiVersion: lifecycle.cmg.io/v1alpha1` and `kind: ChartLifecycle`.
`spec.validation` declares environments, composed values, triggers, policies,
and default-true `validators.kubeconform` / `validators.policy` gates.
`spec.clusterTest` declares live-cluster install profiles and whether each runs
Helm test hooks;
`dependentTests` lists chart/profile tests that should rerun when this chart
changes. Either capability can be absent or explicitly disabled, and
`spec.enabled` pauses both.

See
[`tests/fixtures/charts/passing-app/chart-lifecycle.yaml`](tests/fixtures/charts/passing-app/chart-lifecycle.yaml)
for a minimal validation example. The lifecycle resource intentionally remains
in packaged charts so later lifecycle automation consumes the same
authoritative intent.

`charts/` is the default managed-chart directory, not a fixed repository
layout. Set `CHART_MANAGER_CHARTS_DIR` to a repository-relative path such as
`deploy/helm` to move the entire chart tree. Discovery, Git change
classification, lifecycle planning and diagnostics, validation, cluster
services, upgrades, and Grafana dashboard discovery all consume this same
Pydantic setting. Absolute paths and `.`/`..` traversal are rejected.

CLI operational logs are rendered to stderr, leaving stdout safe for JSON and
pipelines. The Pydantic `Settings.log_level` default is `INFO`; override it
with `CHART_MANAGER_LOG_LEVEL=DEBUG` (or `WARNING`, `ERROR`, or `CRITICAL`) for
more or less detail. Text logs include a UTC timestamp and the module, function,
and line that emitted each record. Set `CHART_MANAGER_LOG_FORMAT=json` for
structured records with `timestamp`, `level`, `logger`, `module`, `function`,
`line`, and `message` fields. Upgrade logging includes its plan, sanitized
Renovate stdout/stderr, and the final outcome; credentials are never logged.

Changed chart files are selected through the existing `triggers:` glob-to-environment
mapping. Intentional exclusions belong in the additive, chart-relative
`triggerIgnores:` glob list. Files matching neither are reported as trigger
coverage gaps; `unmatchedChanges: all-environments` additionally fans those
files out to every declared environment.

## Troubleshooting

- kind nodes report `NotReady` — expected until cilium installs as the CNI.
- `kind: command not found` or cluster creation hangs — Docker Desktop, Colima, or OrbStack must be running before you invoke any kind task.
- `mise: command not found` — install [`mise`](https://mise.jdx.dev), then run `mise trust` in the repo root.

## Going deeper

- [`docs/renovate-upgrades.md`](docs/renovate-upgrades.md) — authenticated,
  isolated Renovate upgrades, dependency coverage, versioning, and callback
  security.
- [`docs/MENTAL_MODEL.md`](docs/MENTAL_MODEL.md) — how the pieces fit together.
- [`docs/chart-lifecycle-spec.md`](docs/chart-lifecycle-spec.md) — lifecycle intent
  and compiled action plans.
- [`docs/validate-pipeline-plan.md`](docs/validate-pipeline-plan.md) — design rationale for the validate pipeline.
