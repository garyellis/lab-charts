# Where a type belongs

This document answers one question: **where does a type belong?**
`lint-imports` enforces the import contracts in `[tool.importlinter]` in
`pyproject.toml`; ADR-0001 and ADR-0002 record why. This is the prose behind
those rules.

## The shape

```text
chart_manager/
  main.py         composition root: global options, the command tree, _outcome_for
  commands/       one package per CLI subcommand; leaves and composites (ADR-0002)
  cli/            toolkit below commands: output, streams, options, Container
  shared/         what two or more commands use: cluster > charts | events > workspace
  settings.py     process configuration and DEFAULT_CLUSTER_NAME
  integrations/   every call to the outside world, one module per system
  api/            authored, versioned YAML contracts
  plumbing/       errors, exit codes, YAML, paths, command runner, progress
```

Each row imports only from rows below it, except that `settings.py`,
`integrations/` and `api/` share one tier and don't import each other: an
adapter is handed resolved inputs, never an authored API type.

The policy and algorithms over `api/` models and `Chart.yaml` live in
`shared/` and `commands/`:

| Module | What it decides |
|---|---|
| `shared/charts/chart.py` | `load_chart`: one `Chart` from its directory, `Chart.yaml` and lifecycle, names agreeing; `chart_names` |
| `shared/charts/dependencies.py` | Whether materialized chart dependencies are stale |
| `shared/charts/dependency_update.py` | Whether to run `helm dependency update` (only when stale) and its default timeout |
| `shared/charts/lifecycle.py` | Loading `chart-lifecycle.yaml`, identity agreement, the `require_*` capability gates |
| `shared/charts/chart_tests.py` | `ChartTestCatalog`: charts composed with their enabled chart tests |
| `shared/charts/install_plan.py` | Dependency resolution and install order |
| `commands/local/targets.py` | Loading `LocalStack`; resolving a `local` target |
| `shared/cluster/local_cluster.py` | Loading `LocalCluster` and checking the paths it names |
| `shared/cluster/session.py` | Provision, attach, stop and tear down a kind cluster |
| `shared/cluster/converge.py` | The one release install and its readiness wait; `installed()` |
| `shared/cluster/bootstrap.py` | The LocalCluster's ordered bootstrap releases, through `converge` |
| `shared/workspace.py` | Fixed-marker discovery; loading and compiling immutable repository policy |

## What `api/` is for

`api/` owns the authored, versioned YAML contracts:

| Version | Kind | Module |
|---|---|---|
| `chartmanager.io/v1alpha1` | `ChartLifecycle` | `api/v1alpha1/chart_lifecycle.py` |
| `chartmanager.io/v1alpha1` | `ChartWorkspace` | `api/v1alpha1/chart_workspace.py` |
| `chartmanager.io/v1alpha1` | `LocalCluster` | `api/v1alpha1/local_cluster.py` |
| `chartmanager.io/v1alpha1` | `LocalStack` | `api/v1alpha1/local_stack.py` |

Each kind module and its shared vocabulary define the complete accepted YAML
shape, with no loader or planner in the way. A change under `api/` can break a
document someone already wrote and so deserves API review; a change to a
compiled plan or execution result cannot.

Consumers import the explicit version:

```python
from chart_manager.api.v1alpha1.chart_lifecycle import ChartLifecycle
```

There are no versionless re-exports, so a future `v1beta1` cannot silently
change what an existing consumer parses.

## The placement test

Ask in order:

1. Can a user author this field in YAML? Probably an API type.
2. Would changing it break an existing YAML document? It belongs in `api/`.
3. Does deciding it require the repository, another document, the
   filesystem, a cluster, or an external command? It belongs outside `api/`.
4. Is it created only after authored intent is resolved or compiled? It
   belongs to the command or shared package that produces it.

Worked examples where the halves look like one thing:

- **`spec.chartTest`** — shape is API (`helmTest` is the wire format), but
  "profile `minimal` is not declared, here are the ones that are" is a
  catalog lookup raising the user-facing `SpecError`, so it lives in
  `shared/charts/lifecycle.py` with the other `require_*` gates. An API
  model that raised `SpecError` would know what a CLI exit code is.
- **`spec.validation`** — shape is API; `selected_row()` resolves it in
  `commands/validate/select.py` because choosing between an
  explicit namespace and a `${env}` substitution is interpretation of an
  already-valid document.
- **`LifecyclePlan`** — looks like an API type, is not one. Nobody authors
  it; the compiler produces it. It lives in `commands/test/models.py`
  and projects through `commands/test/wire.py`.

## What stays out of `api/`

Absolute or existence-checked paths. Helm metadata from `Chart.yaml`.
Name-agreement checks. Cross-resource references. Capability selection.
Resolved namespaces and release names. Dependency graphs and install order.
Compiled plans, worklists, results. Command execution and cluster
observation. `ChartWorkspace` owns authored repository-relative layout;
compiled absolute paths, existence checks, symlink containment, and
cross-resource dependencies belong to `shared/workspace.py`.

## Workspace boundary

Repository-bound commands resolve `CHART_MANAGER_ROOT`/operator config first
(which must itself hold the marker), then the nearest ancestor containing
`.chart-manager/workspace.yaml`. There is no fallback: with no marker,
`Container.workspace()` raises `WorkspaceNotFoundError` (exit 5). The
`Container` compiles one `RepositoryWorkspace` per invocation for
chart discovery, local resources, validation policy and render locations, `plan`,
publishing, upgrades/finalization, and Grafana discovery.

`RepositoryWorkspace` is the resolved root, `metadata.name`, and the validated
`ChartWorkspaceSpec` (`workspace.spec`), plus derived path and fanout helpers.
The loader also rejects layout paths that resolve outside the root and a
`renderDir` with a symlink component. An explicit chart target (`chart test
<chart>`, `chart validate <chart>`) re-points `chartsDir` at the chart's parent
with `workspace.with_charts_dir(path)`, which re-runs the same checks.

`version`, `event`, `promote`, `grafana dashboard export`, and
`grafana dashboard lint --path` never ask for the workspace. Without one,
`doctor` skips its schema checks with the reason; an invalid `workspace.yaml`
fails it with exit 3.

Repository policy is checkout-owned. Machine settings such as kube context,
Docker host, timeouts, logging, credentials, and backend endpoints remain in
`Settings`, which carries no layout and forbids unknown config keys;
`load_settings()` reports a bad key or value as a `SpecError` (exit 3).

## Rules `api/` must obey

- Imports: standard library, Pydantic, and side-effect-free lexical helpers
  from `plumbing` (`names.py`, `paths.py`) only. Never `commands`, `shared`,
  `integrations`, `cli`, `main`, `settings`, Rich, or Typer.
- Validators raise `ValueError` or Pydantic errors — never `SpecError`.
  Translating a decode failure into a user-facing diagnostic is the
  loader's job.
- No filesystem, repository, cluster, or adapter work.

The `Tiers point down` contract enforces the imports inside `chart_manager`;
the rest are review rules.

## A note on shared bases

`api/v1alpha1/common.py` deliberately has **two** bases. `ChartLifecycle` and its
envelope are `strict=True`; the capability specs nested inside are not, so
`spec.validation.enabled: "true"` is coerced today while
`spec.enabled: "true"` is rejected. Collapsing them would reject YAML that
currently parses. The kinds likewise keep separate metadata models:
lifecycle names allow any non-padded string, local resource names must be
DNS labels. Share a base only where behavior is provably identical —
tidiness is not a reason to change what a user's file may say.
