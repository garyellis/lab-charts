# Coding standards

How chart-manager code and tests should be written. Reviewers apply this to every diff;
implementers check their diff against it before finishing. These are strong defaults: deviate
when it's clearly better, and say why in the PR.

## Review questions

Ask these of every diff first.

- **Does this answer an existing question a second time?** Look for an existing function, loader
  or adapter that already does it, and call that instead.
- **Does each new Protocol, parameter, helper or rule have a second real caller?** One caller
  means it is speculative; inline it. Review findings that add code go to the user with their
  cost, and aren't applied automatically.
- **Is it where a human would look?** Two checks: the code is in `commands/<subcommand>`,
  `shared/<capability>` or `integrations/<system>` as ADR-0001 places it, and it uses the
  `GLOSSARY.md` name for each concept, never one listed under _Avoid_.

## Design defaults: code a human can read, own and debug

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
   There are no outside users, so CLI flags, JSON output and `api/` YAML may be renamed; update
   every caller, chart and CI step in the same PR.
8. **No new global state without a reason.** No module-level caches, singletons or memoized
   loaders unless the user asks or a measured hot path needs it. The per-invocation `Container`
   in `cli/_container.py` is an intentional, user-approved exception. Don't remove it.
9. **Comments say what, plus any non-obvious why, in a few lines.** Design history, review
   rationale and "we considered X" go in the PR or issue, not in code or config comments. A
   diff's new comment lines should not outweigh its new code lines.
10. **Readable over clever.** Prefer a flat function to a new class and a parameter to a new
    protocol. Add a single-caller helper only when it names a real concept. Don't trade away
    performance or reliability for brevity, and don't optimise speculatively. In rebuilt
    packages, name entry points after what they do (`validate.run`), not `*Service`.

## Integrations

Every call to the outside world is an adapter in `integrations/`, one module per system.

- **Deep, not wide.** Few methods, each returning an answer (`ReleaseStatus`, `TestResult`)
  rather than raw command output. Add a method when a real caller needs it; delete it when the
  last caller goes.
- **Wiring is required.** The runner or client is a required argument.
- **No policy.** An adapter reports what is true; deciding what to do about it belongs to the
  calling package.
- **Imports** only `plumbing/` and `api/`.
- **Protocols only for two or more real adapters.** Tests fake external tools at the command
  runner (`FakeCommandRunner`), not with a Protocol per tool. Today only the event store has a
  Protocol.

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
- **Keep setup short and shared** with the `tests/conftest.py` helpers: `chart_root` and
  `make_chart` for chart trees, `cli(...)` to invoke the CLI, `FakeCommandRunner` for external
  commands, and `workspace_for` / `write_workspace` for workspaces. Move a helper used by two
  modules to `tests/conftest.py` instead of copying it.
- **Stay hermetic.** No dependence on the cwd, the real `.chart-manager/`, the network or a
  cluster. Real tools belong only under `@pytest.mark.integration`.

Consolidation examples from the current suite (`tests/`):

- `test_kind_port_mappings.py::test_container_host_ports_*`: eight tests that differ only in
  the docker payload. Make it one parametrized table of (ps output, inspect output, expected).
- Same behavior at several layers:
  - `commands/promote/test_cli.py::test_timeout_ordering_violation_is_a_clean_domain_error`
    re-tests the `MonitorRequest` validation.
  - `test_layering.py`: the `*_is_discoverable`, `*_fires_on_*`, `*_stays_quiet_*` and
    `*_allowlist_*` tests check the checker.
