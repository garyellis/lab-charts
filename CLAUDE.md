@AGENTS.md

## Claude Code specifics

AGENTS.md above is the canonical guidance. This section only adds what is specific to Claude Code.

- **Charts:** use the `helm-chart-creator` skill to add or bump a chart. The `charts/` contract
  and validate command in AGENTS.md still apply.
- **Stacked PRs:** use the `gh-stack` skill to create, push and sync the `(#NN, k/n)` stack.
- **Subagents and review rounds:** the rules in AGENTS.md apply to every subagent you spawn and
  to every review round, not just the first pass. Review findings, including those from review
  agents or `/code-review`, aren't automatically in scope; weigh each one under Scope.
