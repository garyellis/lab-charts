# AGENTS.md

Helm wrapper charts in `charts/` and `chart-manager`, a Python CLI in `src/chart_manager/` that
renders, validates and tests them on kind clusters. CI runs the same commands. For CLI usage see
`README.md`, and for where a type belongs see `docs/architecture.md`. Don't restate either here
or in code.

## Layout and layer arrows

```text
cli/ (surface) -> services/ -> api/ + domain/ + integrations/
                           domain/ -> api/ + plumbing/
                              api/ -> plumbing/ (pure helpers only)
```

- `composition.py` is the only place outside `services/` and `integrations/` that builds
  adapters. Surfaces get services from `Container` (`cli/_container.py`).
- `plumbing/` is generic (errors, exit codes, YAML, paths) and imports no higher layer.
- Enforcement already exists: TID251 in `pyproject.toml` and `src/chart_manager/domain/.ruff.toml`,
  plus `tests/test_layering.py`. Work within it rather than adding to it.

## Commands (from `pyproject.toml`, `.mise.toml` and `.github/workflows/ci.yaml`)

```bash
uv run --extra dev pytest -q                      # unit suite; addopts exclude integration
uv run --extra dev pytest -q -m integration tests/integration  # needs helm, kubeconform, kyverno
uv run --extra dev ruff check src/ tests/
uv run --extra dev ruff check --select TID251 src/   # layer contract (CI `layering` job)
uv run --extra dev mypy src/chart_manager            # production code only
mise run check     # CI fast gate: actionlint, chart contracts, ruff, mypy, dashboard lint, pytest
```

CI's integration step runs four of the five files under `tests/integration/`:
`test_chart_lifecycle_packaging.py`, `test_manifest_validation_schema_e2e.py`,
`test_crd_schema_constraints.py` and `test_schema_precedence.py`. It skips
`test_manifest_validation_policy_e2e.py`, which needs kyverno.

**Done** means `mise run check` passes, plus the integration run if you touched rendering,
schemas or packaging. Report what you actually ran.

## Design defaults: code a human can read, own and debug

These are strong defaults. Deviate when it's clearly better, and say why in the PR.

1. **Prefer deleting to adding.** For any refactor, report src and tests net line counts from
   `git diff --stat <base> -- src tests` in the PR. If a "simplify" task grew src, explain why.
2. **One place answers each question.** Read the model directly (`workspace.spec.charts_dir`)
   rather than mirroring its fields as pass-through properties or adding a second loader.
3. **No defaults that hide wiring.** Make path, root, client and settings parameters required.
   Avoid `= None` that falls back to `Path.cwd()`, a global, or a re-read, so a wiring mistake
   shows up as a type error.
4. **Fail loudly in one line.** Raise the matching `plumbing/errors.py` type, naming the bad
   value or path and the fix, e.g. `SpecError(f"{path}: charts_dir {value!r} does not exist")`.
   Skip helpers that rewrite library messages, guess which source a value came from, or re-read
   config just to word an error. Pydantic `extra="forbid"` errors can pass through as they are.
5. **Catch at the boundary.** `cli/main.py::_outcome_for` maps exceptions to `Outcome`, and
   `plumbing/exit_codes.py` maps `Outcome` to a number. Services raise; cli exits. Log with the
   module's `_LOG` at I/O boundaries, using %-style args.
6. **Proportionality.** Enforce a rule once, with a type, a ruff rule or one plain test. Small,
   obviously proportionate checks are fine. Don't add AST-scanning machinery, tests of tests,
   leak tables, or a second enforcement of an existing rule. A nested `.ruff.toml` replaces the
   root tables (`banned-api`, `per-file-ignores`), so avoid adding one.
7. **No shims.** No env-var tombstones, deprecated aliases or "legacy" fallbacks unless asked.
   When something is removed, update its callers in the same PR.
8. **No new global state without a reason.** No module-level caches, singletons or memoized
   loaders unless the user asks or a measured hot path needs it. The per-invocation `Container`
   in `cli/_container.py` is an intentional, user-approved exception. Don't remove it.
9. **Comments say what, plus any non-obvious why, in a few lines.** Design history, review
   rationale and "we considered X" go in the PR or issue, not in code or config comments. A
   diff's new comment lines should not outweigh its new code lines.
10. **Readable over clever.** Prefer a flat function to a new class and a parameter to a new
    protocol. Add a single-caller helper only when it names a real concept. Don't trade away
    performance or reliability for brevity, and don't optimise speculatively.

## Scope

- **Implement the issue as written.** Its stated approach is a decision. Extras (cleanup,
  hardening, adjacent bugs) go to the user as a one-line question or a follow-up issue. Small
  real bugs in lines you're already touching can be fixed; call them out.
- **Respect explicit rejections** ("no shim", "no fallback") in every later round, under any name.
- **Review findings aren't automatically in scope.** If a fix adds a branch, parameter, helper or
  concept and isn't a correctness bug, list it for the user with its cost.
- **Before finishing, step back.** Re-read the issue and the whole `git diff <base>...HEAD`.
  In the PR, say whether it does what was asked, whether it reads simpler, the net line
  counts, and what you left out. For a stack, judge it as a whole against `main`.

## Tests

The suite has about 1,570 test functions in 122 files and about 39k lines, roughly 2,100 cases
once parametrize expands them. Much of it is redundant, so treat test lines as a cost.

- **Test behavior once, at the highest stable boundary.** If the service owns the logic, test
  the service. Test the CLI only for what it adds: flag parsing, output mode, exit code.
- **Parametrize instead of copying** when tests differ by one input and one expected value.
- **Use public entry points, not private helpers.** Treat `from x import _helper` in a test as
  a smell.
- **Assert outcomes, not wording.** Check the exception type, exit code or returned data. Match
  message text only for user-facing errors, and then just the key value, not the sentence.
- **Don't test prose or the test machinery itself**: module docstrings, comment content, or
  "the rule fires on a synthetic leak" tests.
- **Delete tests whose behavior is covered elsewhere.** Don't weaken, skip or `xfail` an
  assertion just to get green.
- **Leave each test module you touch the same size or smaller where you can.** Fold
  near-duplicates while you're in there. A refactor shouldn't add more test lines than src lines
  it removes unless the PR says why. A new test should cover a behavior nothing else does.
- **Keep setup short and shared:**
  - `chart_root` and `make_chart` for chart trees
  - `cli(...)` to invoke the CLI
  - `FakeCommandRunner` for external commands
  - `workspace_for` and `write_workspace`, which arrive with the #106 stack and aren't on
    `main` yet (check they exist)

  Move a helper used by two modules to `tests/conftest.py` instead of copying it.
- **Stay hermetic.** No dependence on the cwd, the real `.chart-manager/`, the network or a
  cluster. Real tools belong only under `@pytest.mark.integration`.

Consolidation examples from the current suite (`tests/`):

- `test_helmrelease_monitor_service.py::test_request_validation_rejects_*`: five near-identical
  `MonitorRequest` rejections that could be one parametrized table. The
  `..._legacy_duration_string` test guards a migration aid; drop it along with the shim.
- `test_kind_port_mappings.py::test_container_host_ports_*`: eight tests that differ only in
  the docker payload. Make it one parametrized table of (ps output, inspect output, expected).
- Same behavior at several layers:
  - `test_cli_helmrelease.py::test_wire_module_does_not_import_rich` repeats layering rule (b).
  - `test_cli_helmrelease.py::test_timeout_ordering_violation_is_a_clean_domain_error`
    re-tests the `MonitorRequest` validation.
  - `test_layering.py`: 15 of its 34 tests (`*_is_discoverable`, `*_fires_on_each_synthetic_leak`,
    `*_stays_quiet_*`, `*_allowlist_exempts_*`) check the checker.

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
