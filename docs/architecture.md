# Layers: API, domain, services

This document answers one question: **where does a type belong?**
Enforcement lives in `tests/test_layering.py` and the TID251 tables in
`pyproject.toml` and `src/chart_manager/domain/.ruff.toml`; this is the prose
behind those rules.

## The shape

```text
cli/surfaces -> services -> api + domain + integrations
                         domain -> api + plumbing
                            api -> plumbing (pure helpers only)
```

`integrations/` shares a rank with `api/` and `domain/` because services
depend on all three — not because an adapter should reach for an authored API
type. Adapters are handed resolved service inputs.

`domain/` is a top-level package holding policy and algorithms over `api/`
models plus the schemas this project does not own (`Chart.yaml`,
`Chart.lock`). It may not import `services/`, `integrations/`, or `cli/` —
enforced by its `.ruff.toml` and `test_domain_does_not_import_upward`.

| Module | What it decides |
|---|---|
| `domain/charts.py` | Helm metadata read from `Chart.yaml`; `ChartRepository` |
| `domain/chart_deps.py` | Whether materialized chart dependencies are stale |
| `domain/lifecycle_policy.py` | Loading `chart-lifecycle.yaml`, identity agreement, the `require_*` capability gates |
| `domain/cluster_tests.py` | `ClusterTestCatalog`: charts composed with their enabled cluster tests |
| `domain/install_plan.py` | Dependency resolution and install order |
| `domain/local_resources.py` | Loading `LocalCluster`/`LocalStack`; resolving a CLI target |

## What `api/` is for

`api/` owns the authored, versioned YAML contracts:

| Group / version | Kinds | Module |
|---|---|---|
| `lifecycle.chartmanager.io/v1alpha1` | `ChartLifecycle` | `api/lifecycle/v1alpha1.py` |
| `local.chartmanager.io/v1alpha1` | `LocalCluster`, `LocalStack` | `api/local/v1alpha1.py` |

Each module is the complete accepted shape of its YAML — field names,
aliases, defaults, enums, every rule decidable from a single document — with
no loader or planner in the way. The boundary exists because a change under
`api/` can break a YAML document someone already wrote and so deserves API
review; a change to a compiled plan or execution result cannot.

Consumers import the explicit version:

```python
from chart_manager.api.lifecycle.v1alpha1 import ChartLifecycle
```

There are no versionless re-exports, so a future `v1beta1` cannot silently
change what an existing consumer parses.

## The placement test

Ask in order:

1. Can a user author this field in YAML? Probably an API type.
2. Would changing it break an existing YAML document? It belongs in `api/`.
3. Does deciding it require the repository, another document, the
   filesystem, a cluster, or an external command? It belongs outside `api/`.
4. Is it created only after authored intent is resolved or compiled? Domain
   or service type.

Worked examples where the halves look like one thing:

- **`spec.clusterTest`** — shape is API (`helmTest` is the wire format), but
  "profile `minimal` is not declared, here are the ones that are" is a
  catalog lookup raising the user-facing `SpecError`, so it lives in
  `domain/lifecycle_policy.py` with the other `require_*` gates. An API
  model that raised `SpecError` would know what a CLI exit code is.
- **`spec.validation`** — shape is API; `resolve_namespace()` is in
  `services/manifest_validation/namespaces.py` because choosing between an
  explicit namespace and a `${env}` substitution is interpretation of an
  already-valid document.
- **`LifecyclePlan`** — looks like an API type, is not one. Nobody authors
  it; the compiler produces it. It lives in `services/lifecycle/models.py`,
  projects through `wire.py` with a `schema_version`, and its wire shape is
  guarded by `tests/test_wire_contracts.py`.

## What stays out of `api/`

Absolute or existence-checked paths. Helm metadata from `Chart.yaml`.
Name-agreement checks. Cross-resource references. Capability selection.
Resolved namespaces and release names. Dependency graphs and install order.
Compiled plans, worklists, results. Command execution and cluster
observation. Filesystem *layout* too: `LIFECYCLE_FILENAME` and friends
describe where documents are found, not what a document may say.

## Rules `api/` must obey

- Imports: standard library, Pydantic, and side-effect-free lexical helpers
  from `plumbing` (`names.py`, `paths.py`) only. Never `domain`, `services`,
  `integrations`, `cli`, the composition root, settings, Rich, or Typer.
- Validators raise `ValueError` or Pydantic errors — never `SpecError`.
  Translating a decode failure into a user-facing diagnostic is the
  loader's job.
- No filesystem, repository, cluster, or adapter work.

`tests/test_layering.py` enforces each rule, including a clean-subprocess
probe that catches an allowlisted helper growing an import, with controls
proving the guards still fire on a synthetic violation.

## A note on shared bases

`api/base.py` deliberately has **two** bases. `ChartLifecycle` and its
envelope are `strict=True`; the capability specs nested inside are not, so
`spec.validation.enabled: "true"` is coerced today while
`spec.enabled: "true"` is rejected. Collapsing them would reject YAML that
currently parses. The two API groups likewise keep separate metadata models:
lifecycle names allow any non-padded string, local resource names must be
DNS labels. Share a base only where behavior is provably identical —
tidiness is not a reason to change what a user's file may say.
