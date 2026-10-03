---
status: accepted (2026-10-02)
---

# Organise chart-manager by feature package, not by layer

The code is split by layer, but it changes by feature, and the layer checks did not stop
concepts from being duplicated. We will group code by what the user does, with a few shared
packages for capabilities that more than one command needs.

## Context

- 40% of non-merge src commits touch 3 or more layers. Feature PRs #84, #89 and #37 touched
  all 8 top-level areas. `chart validate` imports 55 of 161 modules.
- The layer checks (about 1,410 lines: `tests/test_layering.py`, the TID251 tables,
  `domain/.ruff.toml`) enforce import direction, not cohesion. Duplicates passed them:
  chart + `chart-lifecycle.yaml` loading is copied 8 times, and release install is written 3
  times (`bootstrap.py`, `development/service.py`, `lifecycle/cluster_executor.py`).
- Deciding between `api/` and `domain/` became a classification exercise: types moved between
  them 12+ times, and the API group was split in #36 and merged back in #81.

## Decision

Two kinds of package:

- **Command packages**, one per thing a user does: `validate/` (render, schemas, kubeconform,
  kyverno, `schemas sync`), `chart_test/` (chart test/teardown; not `test/`, which clashes
  with pytest), `local/` (local up/down/reset/status, expose), `plan/` (impact, CI matrix),
  `publish/`, `upgrade/`, `helmrelease/` (promote/monitor/test, pending #124).
- **Shared packages**, one per capability used by 2 or more commands, about three in total:
  `charts/` (the one loader: chart + `chart-lifecycle.yaml`, identity, deps), `cluster/`
  (provision kind, converge a release), `tools/` (helm, kubectl, kind, git adapters that
  return answers, not raw command output).
- `api/` (authored YAML contracts) and `plumbing/` (errors, yaml, paths, command runner) stay
  as they are.

Rules:

1. The command tells you the package. Anything used by more than one command goes in the
   shared package named after the capability.
2. Each package owns its whole flow, including its Typer command (`<pkg>/cli.py`).
   `cli/main.py` only registers commands.
3. Command packages may import shared packages, never each other. One exception: `plan/`
   reads the selection logic of `validate/` and `chart_test/`. This rule replaces most of
   `test_layering.py`; "only `*/cli.py` imports typer" is one ruff entry.
4. Flat inside a package: a flow module, its models, `cli.py`. No sub-layers.
5. A new shared package needs two real users.

`domain/` goes away: `charts.py`, `chart_deps.py`, `lifecycle_policy.py`, `install_plan.py`
and `cluster_tests.py` move to `charts/`; `local_resources.py` moves to `local/`.

## Consequences

- A bug lands in one obvious package. A missing cert-manager webhook gate in chart test goes
  to `cluster/`, which fixes local up too. A wrong exit code for missing helm during validate
  goes to `validate/`. A wrong CI matrix goes to `plan/`. The weak spot is the shared packages
  ("`cluster/` or `chart_test/`?"), so keep them few and lean on the concept map (#122).
- Migration goes feature by feature, deleting old modules in the same stack. The owner is the
  only user, so CLI flags, `-o json` shapes, exit codes and `api/` YAML (including kind and key
  names) may change, provided CI, the charts and the integration tests change with them in the
  same PR, with no shims. The integration tests are the behaviour check. The pilot is `validate/`
  (#125). No new enforcement while migrating.
- `docs/architecture.md`, the TID251 tables and `test_layering.py` are rewritten as packages
  land (#121).

## Considered options

- Keep layers and add checks: rejected; checks enforce direction, not cohesion, which is what
  caused the drift.
- One package per CLI command, no shared packages: rejected; it brings back the duplicated
  install loop in `chart_test/` and `local/`.
- Rewrite from scratch: rejected; it loses hard-won fixes and drifts again unless the review
  loop is fixed (#123).
