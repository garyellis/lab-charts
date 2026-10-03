@AGENTS.md

## Claude Code specifics

AGENTS.md above is the canonical guidance. This section only adds what is specific to Claude Code.

- **Charts:** use the `helm-chart-creator` skill to add or bump a chart. The `charts/` contract
  and validate command in AGENTS.md still apply.
- **Stacked PRs:** use the `gh-stack` skill to create, push and sync the `(#NN, k/n)` stack.
- **Subagents and review rounds:** AGENTS.md applies to every subagent you spawn and every review
  round. Give review agents `CODING_STANDARDS.md` and `GLOSSARY.md`. Review findings, including
  those from review agents or `/code-review`, aren't automatically in scope; weigh each one
  under Scope.

## Agent skills

### Issue tracker

Issues live in GitHub Issues for garyellis/lab-charts, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: root `GLOSSARY.md` plus `docs/adr/`. See `docs/agents/domain.md`.
