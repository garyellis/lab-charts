# AGENTS.md

Helm charts in `charts/` and `chart-manager`, a Python CLI in `src/chart_manager/` that
renders, validates and tests them on kind clusters, and promotes them to k8s environments. CI runs the same commands.
Don't restate these docs here or in code:

- `README.md`: CLI usage.
- `CODING_STANDARDS.md`: how code and tests should be written. Review your diff against it
  before finishing.
- `GLOSSARY.md`: the name to use for each concept.
- `docs/adr/`: decisions not to re-argue. ADR-0001 sets the package layout.
- `docs/architecture.md`: where a type belongs in the current layout.

## Layout

- Code is grouped by command (`commands/`), shared package (`shared/`) and external system
  (`integrations/`). ADR-0001 places each module; ADR-0002 says which command may import which.
- `main.py` is the composition root and declares the whole command tree. Each command package
  wires its own adapters from the `Container`'s settings, workspace and runner.
- `lint-imports` enforces the contracts in `[tool.importlinter]` in `pyproject.toml`. Work
  within them rather than adding enforcement.

## Commands (from `pyproject.toml`, `.mise.toml` and `.github/workflows/ci.yaml`)

```bash
uv run --extra dev pytest -q                      # unit suite; addopts exclude integration
uv run --extra dev ruff check src/ tests/ .github/scripts/validation_cache.py
uv run --extra dev lint-imports                      # import contracts
uv run --extra dev mypy src/chart_manager            # production code only
mise run check     # CI fast gate: lint-imports, actionlint, ruff, mypy, vulture, pytest
mise run check:integration  # integration tests; a missing tool fails them
mise run check:charts       # chart source contracts, dashboard lint
```

**Done** means `mise run check` passes, plus `mise run check:integration` if you touched
rendering, schemas or packaging. Report what you actually ran.

## Scope

- **Implement the issue as written.** Its stated approach is a decision. Extras (cleanup,
  hardening, adjacent bugs) go to the user as a one-line question or a follow-up issue. Small
  real bugs in lines you're already touching can be fixed; call them out.
- **Respect explicit rejections** ("no shim", "no fallback") in every later round, under any name.
- **Review findings aren't automatically in scope.** If a fix adds a branch, parameter, helper or
  concept and isn't a correctness bug, list it for the user with its cost.
- **Before finishing, step back.** Re-read the issue and the whole `git diff <base>...HEAD`.
  In the PR, say whether it does what was asked, whether it reads simpler, the net src and
  tests line counts, and what you left out. For a stack, judge it as a whole against `main`.

## Charts

Each `charts/<name>/` has a `chart-lifecycle.yaml` (the `ChartLifecycle` type in
`api/v1alpha1/`). To add or bump a chart, follow the layout of an existing one. Validate with
`uv run chart-manager chart validate <name> --env <env>`.

## Git and PRs

- Use conventional commits: `type(scope): imperative summary`, e.g. `refactor(workspace): ...`.
- Name branches `refactor-issue-NN-<slug>`, `feat/<slug>`, `fix/<slug>` or `ci/<slug>`. A
  local `refactor` branch exists, so `refactor/...` names fail.
- For multi-step issues, use stacked PRs titled `(#NN, k/n)`, one purpose each.
- Commit, push or open PRs only when asked. Leave unrelated uncommitted files in the main
  checkout alone; they are the user's work in progress.
