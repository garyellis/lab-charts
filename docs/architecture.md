# Layers: API, domain, services

This document answers one question: **where does a type belong?**

The enforcement lives in `tests/test_layering.py` and in the TID251 table in
`pyproject.toml`. This is the prose that explains why those rules are shaped the
way they are.

## The shape

```text
cli/surfaces -> services -> api + domain + integrations
                         domain -> plumbing
                            api -> plumbing (pure helpers only)
```

Read the arrows as guidance, not permission. `integrations/` shares a rank with
`api/` and `domain/` because services depend on all three — not because an
adapter should reach for an authored API type. Adapters are handed resolved
service inputs.

## What `api/` is for

`src/chart_manager/api/` owns the authored, versioned YAML contracts:

| Group / version | Kinds | Module |
|---|---|---|
| `lifecycle.cmg.io/v1alpha1` | `ChartLifecycle` | `api/lifecycle/v1alpha1.py` |
| `local.cmg.io/v1alpha1` | `LocalCluster`, `LocalStack` | `api/local/v1alpha1.py` |

Reviewing one of those two modules shows the **complete** accepted shape of the
corresponding YAML — field names, aliases, defaults, enums, and every rule
decidable from a single document — without opening a loader, a planner, or the
CLI.

That is the whole point of the boundary. A change under `api/` can break a YAML
document someone already wrote, so it deserves API review. A change to a
compiled plan or an execution result cannot, so it does not.

Consumers import the explicit version:

```python
from chart_manager.api.lifecycle.v1alpha1 import ChartLifecycle
```

There are deliberately no versionless re-exports, so a future `v1beta1` cannot
silently change what an existing consumer parses.

## The placement test

Ask these in order:

1. Can a user author this field in YAML? If yes, it is probably an API type.
2. Would changing it break an existing YAML document? If yes, it belongs in
   `api/`.
3. Does deciding it require the repository, another document, the filesystem, a
   cluster, or an external command? If yes, the behavior belongs outside `api/`.
4. Is it created only after authored intent has been resolved or compiled? If
   yes, it is a domain or service type.

## Three worked examples from this repository

Each of these splits is real, and each one is a case where the two halves look
similar enough to be mistaken for one thing.

**`spec.clusterTest` — shape is API, lookup is policy.**
`ClusterTestSpec` and `ClusterTestProfile` live in `api/lifecycle/v1alpha1.py`:
the `helmTest` alias *is* the wire format, so renaming it breaks authored charts.
But "profile `minimal` is not declared, here are the ones that are" is a lookup
against a resolved catalog, and it raises the user-facing `SpecError`. That is
`require_cluster_test_profile()` in `services/domain/cluster_test_policy.py`. An
API model that raised `SpecError` would be an API model that knows what a CLI
exit code is.

**`spec.validation` — shape is API, namespace resolution is interpretation.**
`ManifestValidationSpec` is in `api/lifecycle/v1alpha1.py`. `resolve_namespace()`
is in `services/manifest_validation/namespaces.py`, because picking between an
explicit per-environment namespace and a `${env}` substitution is an application
reading an already-valid document, not a rule about whether the document is
valid.

**`LifecyclePlan` — looks like an API type, is not one.**
It carries an `apiVersion` and a `kind`, which is exactly the trap. But nobody
authors a `LifecyclePlan`; it is what the compiler *produces* from authored
intent. It stays in `services/lifecycle/models.py`, and its wire shape is
guarded by `tests/test_wire_contracts.py` instead.

## What stays out of `api/`

Absolute and existence-checked paths. Helm metadata read from `Chart.yaml`.
Agreement between directory, Helm and lifecycle names. Cross-resource
references. Enabled/disabled capability selection. Resolved namespaces and
release names. Dependency graphs and install order. Compiled actions and plans.
Validation worklists, phase results and run results. Command execution and
cluster observation.

Filesystem *layout* also stays out, even though it looks like configuration:
`LIFECYCLE_FILENAME`, `DEFAULT_LOCAL_CONFIG` and `DEFAULT_STACKS_DIR` describe
where documents are found, not what a document may say.

## Rules `api/` must obey

- It may import the standard library, Pydantic, and side-effect-free lexical
  helpers from `plumbing` (`plumbing/names.py`, `plumbing/paths.py`).
- It must not import `services`, `integrations`, `cli`, the composition root,
  repository settings, Rich, or Typer.
- Its validators raise `ValueError` or Pydantic validation errors — never
  `SpecError`. Translating a decode failure into a user-facing diagnostic is the
  loader's job.
- It performs no filesystem, repository, cluster or adapter work.

`tests/test_layering.py` enforces each of these, including a clean-subprocess
probe that catches an allowlisted helper which later grows an import, and
controls that prove the guards still fire on a synthetic violation.

## A note on shared bases

`api/base.py` is deliberately small, and deliberately has **two** bases rather
than one. `ChartLifecycle` and its envelope are `strict=True`; the capability
specs nested inside it are not, so `spec.validation.enabled: "true"` is coerced
today while `spec.enabled: "true"` is rejected. Collapsing them into one strict
base would have been tidier and would have rejected YAML that currently parses.
The two API groups likewise do not share a metadata model: lifecycle names allow
any non-padded string, local resource names must be DNS labels.

Share a base only where the behavior is provably identical. Tidiness is not a
reason to change what a user's file is allowed to say.
