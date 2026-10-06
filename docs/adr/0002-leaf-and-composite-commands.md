---
status: accepted (2026-10-05)
---

# Command packages are leaves or composites

ADR-0001 made command packages independent, with two exceptions: `plan` may import each
command's `select()` and `doctor` each command's `requirements()`. We name that exception
instead of listing it. A **leaf** command never imports another command. A **composite**
command answers a question about several commands by asking each leaf through its interface.
Today the composites are `plan` and `doctor`.

Rules, enforced by import-linter:

1. A composite imports leaves, never another composite. Composition stays one level deep; if
   two composites need the same thing, it belongs in `shared/`.
2. A composite imports only a leaf's package root (`from chart_manager.commands import
   validate`, then `validate.select`), never a leaf's submodules. The leaf's `__init__.py` is
   its interface.
3. Composites are named in the contract, so becoming one is a reviewed change.

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

- Push composition down into `shared/` (Cargo's `ops`, kubectl's `cmd/util`): rejected;
  selection rules would leave their command, against ADR-0001.
- Push it up into `main.py` or CI (flutter doctor's provider list, Unix pipelines): rejected;
  it adds a second pattern beside plan, which must hold its leaves' result types anyway.
- Per-import named exceptions or a blanket aggregator tier: rejected; the first lists lines
  without a concept, the second lets aggregators reach into leaf internals.
- Composites calling composites (git's porcelain calling porcelain): rejected; it is where
  git says its complexity lives.
