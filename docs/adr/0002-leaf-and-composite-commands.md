---
status: accepted (2026-10-05)
---

# Command packages are leaves or composites

ADR-0001 made command packages independent, with two exceptions: `plan` may import each
command's `select()` and `doctor` each command's `requirements()`. We name that exception
instead of listing it. A **leaf** command never imports another command. A **composite**
command answers a question about several commands by asking each leaf through its interface.
Today the composites are `plan` (each leaf's `select()` and selection type) and `doctor` (only
validate's schema check: `doctor --for` and its per-command requirements table are removed, so
doctor runs every check).

Rules, enforced by import-linter:

1. A composite imports leaves, never another composite. Composition stays one level deep; if
   two composites need the same thing, it belongs in `shared/`.
2. A composite imports only a leaf's package root (`from chart_manager.commands import
   validate`, then `validate.select`), never a leaf's submodules. The leaf's `__init__.py` is
   its interface.
3. Composites are named in the contract, so becoming one is a reviewed change.
4. A leaf's package root is a cheap interface: it re-exports what composites and `main.py`
   read (models, `select`), never `run` or `cli`, and loads no `integrations.*`. A `forbidden`
   contract checks the root `__init__` files only (`as_packages = false`). One counted
   exception: validate's root loads `integrations.kubeconform` for the schema check doctor runs.

`shared/` follows the same shape with one addition, a **floor**. `workspace` and `settings`
are the floor: they import nothing else from `shared/`, and every shared package may use them.
`charts` and `events` are leaves: they import only the floor, never each other. `cluster` is
the composite: it installs charts, so it reads `charts`. Rule 1 holds (a shared composite never
imports another composite); rule 2 does not apply, because a shared package's submodules are
its interface and commands already import them directly. One `layers` contract orders the
rows, so a new shared composite is a reviewed change:

```toml
[[tool.importlinter.contracts]]
name = "Shared: cluster is a composite over the charts and events leaves; workspace and settings are the floor"
type = "layers"
containers = ["chart_manager.shared"]
layers = ["cluster", "charts | events", "workspace | settings"]
```

`main.py` is the composition root and owns the command tree: it mounts every leaf's Typer
callbacks and sub-apps in `--help` order. Leaves don't register themselves, so a leaf never
needs to know its neighbours' place in the help.

```toml
[[tool.importlinter.contracts]]
name = "Leaf commands are independent; composites import only a leaf's root"
type = "independence"
modules = ["chart_manager.commands.*"]
ignore_imports = [
  "chart_manager.commands.plan.** -> chart_manager.commands.*",
  "chart_manager.commands.doctor.** -> chart_manager.commands.*",
]

[[tool.importlinter.contracts]]
name = "Composites never import each other"
type = "independence"
modules = ["chart_manager.commands.plan", "chart_manager.commands.doctor"]
```

Checked with import-linter 2.15: leaf -> leaf, plan -> doctor, doctor -> plan and plan ->
`validate.run` all fail; plan -> `validate` passes. A composite's `__init__.py` must not import
leaves (`plan.**` does not match `plan`), and a name that is also a submodule must be reached
as an attribute (`validate.select`), not imported (`from ... validate import select`).

## Considered options

- Lazy leaf roots (PEP 562 `__getattr__`, Click `LazyGroup`): rejected; import-linter still
  sees the deferred imports, PEP 562 breaks when the lazy name matches a submodule (`run`), and
  PEP 810 needs Python 3.15.
- Each leaf `register()`s itself: rejected; help order became a side effect spread over 12
  files, and catalog would have had to call test's `register` (leaf to leaf).

- Push composition down into `shared/` (Cargo's `ops`, kubectl's `cmd/util`): rejected;
  selection rules would leave their command, against ADR-0001.
- Push it up into `main.py` or CI (flutter doctor's provider list, Unix pipelines): rejected;
  it adds a second pattern beside plan, which must hold its leaves' result types anyway.
- Per-import named exceptions or a blanket aggregator tier: rejected; the first lists lines
  without a concept, the second lets aggregators reach into leaf internals.
- Composites calling composites (git's porcelain calling porcelain): rejected; it is where
  git says its complexity lives.
