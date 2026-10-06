---
status: accepted (2026-10-03)
---

# Organise chart-manager by command and shared package, not by layer

The code is split by layer, but it changes by feature, and the layer checks did not stop
concepts from being duplicated. We group code by what the user does (`commands/`), put
behaviour that two or more commands need in `shared/`, and keep every call to the outside
world in `integrations/`.

## Context

- 40% of non-merge src commits touch 3 or more layers. Feature PRs #84, #89 and #37 touched
  all 8 top-level areas. `chart validate` imports 55 of 161 modules.
- The layer checks (about 1,410 lines: `tests/test_layering.py`, the TID251 tables,
  `domain/.ruff.toml`) enforce import direction, not cohesion. Duplicates passed them:
  chart + `chart-lifecycle.yaml` loading is copied 8 times, and release install is written 3
  times (`bootstrap.py`, `development/service.py`, `lifecycle/cluster_executor.py`).
- Deciding between `api/` and `domain/` became a classification exercise: types moved between
  them 12+ times, and the API group was split in #36 and merged back in #81.
- "Service" meant a layer, 16 class suffixes and a Kubernetes `Service` at once.

## Decision

```text
chart_manager/
  main.py         composition root: the command tree; maps errors to exit codes
  api/v1alpha1/   Kubernetes-style resource shapes only (kinds, fields, validation)
  cli/            output.py, Container
  commands/       validate test publish upgrade promote local plan event catalog grafana doctor
  shared/         charts cluster events workspace.py
  settings.py     process configuration (env, config file) and its defaults
  integrations/   every call to the outside world, one module per external system
  plumbing/       errors, yaml, paths, command runner
```

- **Command packages** (`commands/<name>`) are named after the CLI subcommand: `chart validate`
  is `commands/validate`, `chart test` is `commands/test`, `local up` is `commands/local`.
  `chart list` and `chart show` are `commands/catalog`.
  `promote` has the subcommands `pr`, `monitor` and `test` (one promotion flow, three stages).
  `version` stays one line in `main.py`.
- **Shared packages** (`shared/`) hold behaviour used by two or more commands:
  - `charts/`: everything about a chart directory: `Chart.yaml`, `chart-lifecycle.yaml` with
    names checked, dependencies and their freshness, which charts depend on it, install order.
  - `cluster/`: `provision`, `attach`, `bootstrap`, `converge`, `teardown`. `provision` and
    `attach` return a fixed session (handle plus bound helm and kubectl). `bootstrap` reports
    what it installed. `converge` is the one release install: dependency update, install, wait,
    diagnostics on failure. It waits on the Deployments, StatefulSets and DaemonSets in the
    release's own manifest or labelled with its instance, and on CRDs in it becoming
    `Established`; a chart with only CRs or policies has nothing else to wait on. No chart
    gets its own code: the cert-manager webhook is a Deployment in its manifest. Anything
    more is the chart's `helmTest` or hooks.
  - `events/`: write and query events; the backend is chosen from settings.
  - `workspace.py`: read once per run by the `Container`.
- **`settings.py`** sits at the top level, below `shared/` and beside `api/` and
  `integrations/`: process configuration is not a domain capability. It imports only
  `plumbing/`; `DEFAULT_CLUSTER_NAME` lives here.
- **Integrations** are deep adapters: few methods, each returning an answer rather than raw
  output, with the runner or client a required argument and no policy decisions. A package
  uses as many as it needs. A Protocol goes in front of integrations only where two or more
  real adapters fill it; today that is the event store (Cosmos, DynamoDB, in-memory).
- **`api/`** keeps Kubernetes-style `apiVersion`/`kind` resources for the CLI and a future
  controller. Behaviour (loading, resolving, cross-checks) lives in the packages, so `domain/`
  goes away.

Rules:

1. The command tells you the package; anything two or more commands use is in `shared/`; any
   call to the outside world is in `integrations/`.
2. Each package owns its flow and wires itself from settings, workspace and the command runner.
   Its Typer command is `<pkg>/cli.py`. `composition.py` goes away.
3. Imports, enforced by one import-linter config: command packages are independent of each
   other; `shared/` never imports `commands/`; `integrations/` imports only `plumbing/`;
   inside `shared/`, `workspace` imports nothing else from `shared/`.
   Two exceptions: `plan/` may import each command's `select()` and its result types, and
   `doctor/` may import each command's `requirements()`.
4. Flat inside a package: an entry function `run(request) -> outcome`, its models, `cli.py`.
   Plain functions over `*Service` classes; a class only when it holds real state.
5. A new shared package needs two real users. Selection rules stay with their command.
6. Tests sit at three seams: a command's `run()` (with `FakeCommandRunner` and a `tmp_path`
   workspace), a shared package's own interface, and `cli(...)` for flags, output mode and exit
   code only. Test folders mirror the packages where that helps.

Where today's code goes:

| Today | Goes to |
|---|---|
| `domain/charts.py`, `chart_deps.py`, `lifecycle_policy.py`, `install_plan.py`, `cluster_tests.py`, `_shared.lifecycle_install_plan`, chart-target resolution from `local_resources.py` | `shared/charts` |
| `domain/workspace.py` | `shared/workspace.py` |
| `clusters/environment.py`, `provisioning_hooks.py`, `bootstrap.py`, the 3 install loops, `_shared` OCI and kind helpers, `ExternallySatisfiedLifecycle` | `shared/cluster` |
| `lifecycle/compiler.py`, `cluster_executor.py`, `hooks.py`, `models.py`, the rest of `plan_projection.py`, `clusters/ephemeral.py` | `commands/test` |
| `lifecycle/impact.py`, `services/ci.py` | `commands/plan` |
| `LocalCluster` loading from `local_resources.py` | `shared/cluster` |
| `LocalStack` and target resolution from `local_resources.py`, `clusters/development/` | `commands/local` |
| `manifest_validation/`, `kubeconform_schemas/` | `commands/validate` |
| `helmrelease/` | `commands/promote` |
| `services/events/` writer and query | `shared/events`; adapters to `integrations/` |
| `ClusterHelm`, `ClusterKubectl`, `_ExecutorHelmAdapter`, `expose.py`, `test_layering.py`, TID251 tables, `domain/.ruff.toml` | deleted |

## Consequences

- A bug lands in one obvious package: a wrong readiness wait in chart test is in
  `shared/cluster` and the fix covers `local up`; a wrong exit code for missing helm during
  validate is in `commands/validate`; a wrong CI matrix is in `commands/plan`. The weak spot is
  `shared/` ("cluster or test?"), so keep it small and lean on the concept map (#122).
- Chart test and `local up` change behaviour on purpose: all three install paths share one
  wait and collect diagnostics on any failure.
- The owner is the only user, so CLI flags, `-o json` shapes, exit codes and `api/` YAML may
  change when CI, the charts and the integration tests change in the same PR, with no shims.
  The contract-freeze tests become one test that loads every resource file in the repo (#127).
- Migration order, one stack or one PR with a commit per step: the #124 and #127 deletions;
  `commands/validate` (#125); `commands/test` and `commands/local` together with
  `shared/cluster`; `commands/plan`; `publish`, `promote`, `upgrade`; then delete the empty
  layers. No new enforcement until import-linter replaces the old checks.

## Considered options

- Keep layers and add checks: rejected; checks enforce direction, not cohesion.
- One package per command with no shared packages: rejected; it brings back the duplicated
  install loop in `test` and `local`.
- An adapter lives with its only user: rejected; "outside call → `integrations/`" is simpler
  to predict and keeps adapters reusable.
- Ports or Protocols in front of every integration: rejected; most had one implementation,
  and the seam already exists at the command runner.
- Name the promotion package `rollout`: rejected; ambiguous with Argo Rollouts and
  `kubectl rollout`.
- Rewrite from scratch: rejected; it loses hard-won fixes and drifts again unless the review
  loop is fixed (#123).
