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
uv run chart-manager doctor --for 'chart validate'
uv run chart-manager chart validate --chart grafana --env dev
```

`mise install` pulls every pinned tool. `mise run setup` installs the Python CLI into a uv-managed venv. `doctor` is the preflight: it checks that the binaries, kubecontext, container runtime, and backends a command needs are actually usable, and `--for` narrows it to the prerequisites of one command instead of the whole surface. The final command renders the `grafana` chart for the `dev` environment, validates the manifests against the Kubernetes schema, and runs the policy set declared under `spec.validation` in its `chart-lifecycle.yaml`.

## Daily commands

| Command | What it does |
| --- | --- |
| `uv run chart-manager doctor` | Check every integration's prerequisites — binaries and their versions, kubecontext, container runtime, events backend. `--for '<command>'` checks only what that one command needs. |
| `uv run chart-manager chart validate --chart <name> --env <env>` | Render one chart for one authored env, then run its enabled validators. |
| `uv run chart-manager chart validate --chart <name> --all` | Validate every environment authored for one chart. |
| `mise run validate -- --all` | Same as above, fanned out across every chart and every env declared in the repo. |
| `uv run chart-manager chart test <name-or-path> --profile minimal` | Spin up a local Kubernetes test cluster, install the chart, and run its Helm test hooks when enabled. `--namespace` explicitly overrides the profile namespace. |
| `uv run chart-manager local up --chart <name-or-path> --profile minimal` | Create or start the chart's local cluster, run configured bootstrap releases, and converge the chart. |
| `uv run chart-manager local up --stack <name-or-path>` | Converge a `LocalStack` from `.chart-manager/stacks/<name>.yaml` or an explicit YAML path. |
| `uv run chart-manager local status` | Report whether the local cluster exists, which releases it holds, the URLs it serves, and any host-port drift from `kind-config.yaml`. |
| `uv run chart-manager local down` | Stop the configured local cluster while preserving releases, data, and image caches. |
| `uv run chart-manager local reset --chart <name-or-path>` | Destroy and recreate that chart's cluster, then converge it. Use `--stack` for a stack. |
| `uv run chart-manager chart list` | List every chart wrapper the CLI knows about, with its lifecycle capability status. `mise run charts` is the same command. |
| `uv run chart-manager chart show <name>` | Print one chart's normalized `ChartLifecycle` intent, in a form that diffs against the authored `chart-lifecycle.yaml`. |
| `mise run test` | Run the Python unit tests for the CLI. |
| `uv run chart-manager plan --changed-file charts/<name>/values.yaml` | Explain validation and cluster-test fanout for explicit changed files. |
| `uv run chart-manager chart publish grafana loki --repository oci://harbor.local/charts --version-suffix pr.318.g1a2b3c4` | Prepare a batch, then publish it to an authenticated OCI registry. |
| `uv run chart-manager chart upgrade --path charts/<name>` | Discover Renovate updates in an isolated worktree and open an idempotent chart-upgrade PR. |
| `uv run chart-manager grafana dashboard export <uid> --to charts/grafana-dashboards/dashboards/<folder>/<name>.json` | Pull one dashboard from the kind-deployed Grafana and write canonical JSON for git. |
| `uv run chart-manager grafana dashboard lint` | Lint every committed dashboard against the repository's quality rules. |

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

All `local up`, `local status`, `local down`, and `local reset` targets use the
single `chart-manager` cluster by default. This avoids duplicate Kind clusters
and host-port conflicts from the shared `kind-config.yaml`. Select another
Pydantic-configured `.chart-manager` directory when a different
`LocalCluster` and Kind configuration are needed.

`local status` is a read, not a grade: an absent cluster, an unreachable one,
or a pile of failed releases is the answer and still exits 0, which leaves the
caller to decide what counts as bad —
`chart-manager local status -o json | jq '.releases[] | select(.status!="deployed")'`
is the idiom. Its lookups are the ones the converge path already makes, so the
releases it lists and the URLs it prints are what `local up` saw.

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

Use `uv run chart-manager chart validate --help`. One command covers all three
selections: a single chart, a changed worklist, and a repository-wide run —
which one you get is argv shape, not a separate subcommand.

## Output projections and dry runs

Every command that emits a document takes `-o`/`--output`, and `-o` always
names a **format** — `table`, `json`, `yaml`, plus `md`, `github`, or `all`
where a command has them. The default is `auto`: the table a human reads at a
terminal, and `json` whenever stdout is a pipe, a file, or a CI log. That is
why `chart-manager plan` needs no flag in a workflow and
`chart-manager chart list | jq` works without one. `-o` is also accepted
before the subcommand, where it sets the default for whichever command runs;
the command's own `-o` still wins.

`grafana dashboard export` is the one place where that reading is easy to get
wrong. `-o` there used to name the destination file and now names the format
like everywhere else; the destination is `--to`. There is no alias, so a path
handed to `-o` is a usage error that names `--to` rather than a dashboard
written somewhere surprising. The file `--to` writes is always canonical JSON
whatever `-o` says, because it is the git artifact.

`--dry-run` resolves the same plan the real run would execute and prints it
in `--output` form without touching anything. `local up`, `local down`,
`local reset`, `chart test`, `chart cache clean`, `chart publish`,
`chart upgrade`, and `helmrelease promote` all take it. On `chart test` and
`chart cache clean` that plan is the only document either command produces,
so `-o` is meaningful only alongside `--dry-run`; naming it on its own is a
usage error rather than a flag that is quietly ignored.

## CI

CI uses the same **`chart-manager chart test`** execution path you run
locally. A `prep` job inspects the PR diff and decides which charts
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
`chart-manager plan -o github` instead of maintaining a second fanout
heuristic in YAML.

## Reproducing a CI failure

- Open the failed run and download `rendered-manifests-<run_id>` (validate) or `sandbox-logs-<chart>-<run_id>` (sandbox-test) from the Artifacts panel.
- Reproduce a validate failure locally with `uv run chart-manager chart validate --chart <name> --env <env>`.
- Reproduce a sandbox-test failure locally with `uv run chart-manager chart test <name> --profile minimal`.
- If the failure looks environmental rather than chart-shaped, run `uv run chart-manager doctor --for 'chart test'` first — it names the missing binary or unreachable backend instead of leaving you to infer it from a subprocess error.

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

The accepted shape of that file is defined in one place:
[`src/chart_manager/api/lifecycle/v1alpha1.py`](src/chart_manager/api/lifecycle/v1alpha1.py).
The shape of `.chart-manager/local-cluster.yaml` and `stacks/*.yaml` is
[`src/chart_manager/api/local/v1alpha1.py`](src/chart_manager/api/local/v1alpha1.py).
Reading one of those modules is reading the whole contract — field names,
aliases, defaults and single-document validation — with no loader or planner in
the way. See [`docs/architecture.md`](docs/architecture.md) for why the contract
lives apart from the code that interprets it.

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

Start with `uv run chart-manager doctor`. It is read-only and cluster-free —
every probe either reads local state or asks one short, capped, non-mutating
question of a remote — so it reports a stopped cluster or a dead docker daemon
instead of hanging on it. Each row carries the fix beside the failure, and
`--for '<command>'` narrows the run to one command's prerequisites. The exit
code says which kind of problem it is: `127` when a required binary is not on
`PATH`, `5` when the environment is at fault (no kubecontext, an unreachable
backend), `4` when a tool is installed but broken, and `3` when configuration
is invalid. The most fundamental failure wins.

- kind nodes report `NotReady` — expected until cilium installs as the CNI.
- `kind: command not found` or cluster creation hangs — Docker Desktop, Colima, or OrbStack must be running before you invoke any kind task.
- `mise: command not found` — install [`mise`](https://mise.jdx.dev), then run `mise trust` in the repo root.
- A local URL stopped resolving after editing `kind-config.yaml` — `uv run chart-manager local status` reports the host ports the cluster is missing. Creation-time Kind settings need `local reset`, not `local up`.

## Going deeper

- [`docs/architecture.md`](docs/architecture.md) — where a type belongs: the
  authored API contract versus domain, service and execution types.
- [`docs/renovate-upgrades.md`](docs/renovate-upgrades.md) — authenticated,
  isolated Renovate upgrades, dependency coverage, versioning, and callback
  security.
- [`docs/MENTAL_MODEL.md`](docs/MENTAL_MODEL.md) — how the pieces fit together.
- [`docs/chart-lifecycle-spec.md`](docs/chart-lifecycle-spec.md) — lifecycle intent
  and compiled action plans.
- [`docs/validate-pipeline-plan.md`](docs/validate-pipeline-plan.md) — design rationale for the validate pipeline.
