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

`mise install` pulls every pinned tool. `mise run setup` installs the Python CLI into a uv-managed venv. The final command renders the `grafana` chart for the `dev` environment, validates the manifests against the Kubernetes schema, and runs the policy set declared in its `validate-spec.yaml`.

## Daily commands

| Command | What it does |
| --- | --- |
| `mise run validate -- --chart <name> --env <env>` | Render one chart for one env, then run schema and policy checks against the rendered manifests. |
| `mise run validate -- --all` | Same as above, fanned out across every chart and every env declared in the repo. |
| `mise run kind-test -- <name> --profile minimal` | Spin up an ephemeral kind cluster, do a real `helm install` of the chart, run smoke checks, and tear the cluster down. |
| `mise run charts` | List every chart wrapper the CLI knows about. |
| `mise run test` | Run the Python unit tests for the CLI. |

For the full flag surface on validate, run `uv run chart-manager validate run --help`.

## CI

CI mirrors local exactly: **`mise run validate` and `mise run kind-test` are the same commands the workflow invokes.** A `prep` job inspects the PR diff and decides which charts changed; `validate` runs against the full set; `sandbox-test` fans out as a matrix with one kind job per changed chart so unrelated charts never gate your PR.

```text
prep ──┬── validate
       └── sandbox-test (matrix: one job per changed chart)
```

The fanout heuristic lives in [`.github/workflows/ci.yaml`](.github/workflows/ci.yaml).

## Reproducing a CI failure

- Open the failed run and download `rendered-manifests-<run_id>` (validate) or `sandbox-logs-<chart>-<run_id>` (sandbox-test) from the Artifacts panel.
- Reproduce a validate failure locally with `mise run validate -- --chart <name> --env <env>`.
- Reproduce a sandbox-test failure locally with `mise run kind-test -- <name> --profile minimal`.

## Adding or editing a chart

Each chart owns a `charts/<name>/validate-spec.yaml` that declares its environments, the values files composed for each environment, and the policy set to apply. See [`tests/fixtures/charts/passing-app/validate-spec.yaml`](tests/fixtures/charts/passing-app/validate-spec.yaml) for the canonical minimal example.

## Troubleshooting

- kind nodes report `NotReady` — expected until cilium installs as the CNI.
- `kind: command not found` or cluster creation hangs — Docker Desktop, Colima, or OrbStack must be running before you invoke any kind task.
- `mise: command not found` — install [`mise`](https://mise.jdx.dev), then run `mise trust` in the repo root.

## Going deeper

- [`docs/MENTAL_MODEL.md`](docs/MENTAL_MODEL.md) — how the pieces fit together.
- [`docs/validate-pipeline-plan.md`](docs/validate-pipeline-plan.md) — design rationale for the validate pipeline.
