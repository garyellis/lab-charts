# lab-charts

## What this repo is

A collection of Helm wrapper charts under `charts/`, a Python CLI (`chart-manager`) that renders, schema-checks, and sandbox-tests them on ephemeral kind clusters, and a CI pipeline that runs the same commands. Everything you do locally is what CI does on a pull request.

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
mise run validate -- --chart grafana --env dev
```

`mise install` pulls every pinned tool. `mise run setup` installs the Python CLI into a uv-managed venv. The final command renders the `grafana` chart for the `dev` environment, validates the manifests against the Kubernetes schema, and runs the policy set declared under `spec.validation` in its `chart-lifecycle.yaml`.

## Daily commands

| Command | What it does |
| --- | --- |
| `mise run validate -- --chart <name> --env <env>` | Render one chart for one env, then run schema and policy checks against the rendered manifests. |
| `mise run validate -- --all` | Same as above, fanned out across every chart and every env declared in the repo. |
| `mise run kind-test -- <name> --profile minimal` | Spin up an ephemeral kind cluster, do a real `helm install` of the chart, run smoke checks, and tear the cluster down. |
| `mise run charts` | List every chart wrapper the CLI knows about. |
| `mise run test` | Run the Python unit tests for the CLI. |
| `uv run chart-manager lifecycle plan <name> --workflow validation --profile dev` | Compile authored intent into the exact action DAG without executing it. |
| `uv run chart-manager lifecycle status <name> --workflow cluster-test --profile minimal --live` | Merge cached evidence with read-only Helm and Kubernetes observations. |
| `uv run chart-manager lifecycle impact --changed-file charts/<name>/values.yaml` | Explain validation and cluster-test fanout for explicit changed files. |
| `uv run chart-manager lifecycle doctor` | Validate lifecycle inputs, cross-chart references, and dependency cycles repository-wide. |
| `uv run chart-manager upgrade --path charts/<name>` | Discover Renovate updates in an isolated worktree and open an idempotent chart-upgrade PR. |

For the full flag surface on validate, run `uv run chart-manager validate run --help`.

## CI

CI mirrors local exactly: **`mise run validate` and `mise run kind-test` are the same commands the workflow invokes.** A `prep` job inspects the PR diff and decides which charts changed; `validate` runs against the full set; `sandbox-test` fans out as a matrix with one kind job per changed chart so unrelated charts never gate your PR.

```text
prep ──┬── validate
       └── sandbox-test (matrix: one job per changed chart)
```

Validation selection and the sandbox chart/profile matrix are derived by the
lifecycle impact service. The workflow consumes
`chart-manager ci cluster-test-matrix` instead of maintaining a second fanout
heuristic in YAML.

## Reproducing a CI failure

- Open the failed run and download `rendered-manifests-<run_id>` (validate) or `sandbox-logs-<chart>-<run_id>` (sandbox-test) from the Artifacts panel.
- Reproduce a validate failure locally with `mise run validate -- --chart <name> --env <env>`.
- Reproduce a sandbox-test failure locally with `mise run kind-test -- <name> --profile minimal`.

## Adding or editing a chart

Each managed chart owns one standalone
`charts/<name>/chart-lifecycle.yaml` resource with
`apiVersion: lifecycle.cmg.io/v1alpha1` and `kind: ChartLifecycle`.
`spec.validation` declares environments, composed values, triggers, and
policies. `spec.clusterTest` declares live-cluster install profiles and checks;
`dependentTests` lists chart/profile tests that should rerun when this chart
changes. Either capability can be absent or explicitly disabled, and
`spec.enabled` pauses both.

See
[`tests/fixtures/charts/passing-app/chart-lifecycle.yaml`](tests/fixtures/charts/passing-app/chart-lifecycle.yaml)
for a minimal validation example. The lifecycle resource intentionally remains
in packaged charts so later lifecycle automation consumes the same
authoritative intent.

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
- [`docs/chart-lifecycle-spec.md`](docs/chart-lifecycle-spec.md) — lifecycle intent,
  compiled action plans, evidence, and synthesized status.
- [`docs/validate-pipeline-plan.md`](docs/validate-pipeline-plan.md) — design rationale for the validate pipeline.
