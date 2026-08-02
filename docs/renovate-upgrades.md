# Renovate-driven chart upgrades

`chart-manager chart upgrade` runs Renovate against one wrapper chart and
turns the result into one repeatable pull request.

```bash
uv run chart-manager chart upgrade --path charts/cert-manager
uv run chart-manager chart upgrade --path charts/cert-manager --dry-run -o json
```

Renovate performs its own platform checkout; it never switches branches in,
stages files from, or commits to the caller's checkout. Before starting,
chart-manager verifies the chart and its Renovate inputs have no uncommitted
changes, so unrelated local files cannot become part of the upgrade.

## Authentication

Renovate uses its token for the checkout, branch, and PR. The GitHub CLI is
used only to check whether the chart's branch already has an open PR.

```bash
uv run chart-manager doctor --for 'chart upgrade'
```

checks exactly this command's prerequisites — `git`, the repository, `gh`
auth, the Renovate runtime and config validator, the Renovate token, the
events backend — and prints the fix beside each failure. Renovate also needs
credentials for every private registry the chart references; supply them
through Renovate's documented environment variables or the runner's secret
store, never in chart configuration or arguments.

## Configuration and dependency coverage

Renovate resolves configuration in order:

1. trusted self-hosted policy from `renovate-global.json` (non-standard
   filename so Renovate does not auto-load it a second time);
2. optional chart-local policy from `<chart>/renovate.json`;
3. chart-manager's generated overlay: selected path, grouping, callback;
4. the repository's root `renovate.json`, loaded once after cloning. It is
   trusted and must not weaken the generated chart scope or callback
   restrictions.

Every mutable image in a chart must be visible to Renovate. Prefer the
conventional image repository/tag fields; for nested or templated values,
add the Renovate coverage annotation next to the value. Literal
`image: repository/name:tag` declarations in chart YAML are also recognized.
Extend the extraction tests whenever a new image-value convention appears.

## Version and pull-request policy

The wrapper version bump is deterministic: **major** when any image or chart
dependency has a major update, otherwise **patch**.

Each chart owns the branch namespace `renovate/<chart>/` and holds one branch
in it. Re-running the same proposal reports the existing open PR instead of
creating a duplicate; nothing to update is reported without a branch or PR.

The per-chart prefix is what lets charts upgrade independently: Renovate's
stale-branch pruning is scoped by `branchPrefix` alone, and a chart-scoped
run extracts only one chart, so under a shared `renovate/` prefix every run
would autoclose every other chart's open PR. `branchPrefix`,
`branchPrefixOld`, and `includePaths` are set inside `force` so the root
`renovate.json` cannot re-break that isolation.

The branch name is never re-derived locally: PRs are matched by namespace
prefix and the branch comes from the PR's head ref. Multiple open PRs under
one chart's namespace are reported as a diagnostic, not silently resolved.
A newer version arriving while a PR is open updates it in place; commits
someone pushed to the branch are layered on, not force-pushed over — though
customization belongs in an ordinary PR, since upgrade branches are
machine-owned and may be rebased or pruned.

`--dry-run` performs discovery without pushing a branch or opening a PR. The
final wrapper version exists only once Renovate creates the branch, so it is
absent from dry-run output; otherwise it is read back from `Chart.yaml` on
the pushed branch via the GitHub API. That read-back is also the check that
the finalize callback ran: Renovate records a failed post-upgrade command as
an artifact error and still opens the PR, so a wrapper version matching the
baseline is reported as a diagnostic (Renovate independently flags the PR
with a warning and a failing status check).

`-o json` includes the chart, path, current and proposed versions, branch,
outcome, PR data, and diagnostics. The default `auto` prints a table at a
terminal and `json` in a pipe.

## Renovate callback

`chart-manager upgrade-finalize` is an internal, hidden callback Renovate
invokes after writing its result data — not an operator command. Treat it as
privileged: run only repository-owned, reviewed configuration; pin the
Renovate runtime; restrict the token to the target repository; validate the
callback data path; never execute callback text from dependency metadata.
PR creation stays idempotent if the callback is retried.

## Troubleshooting

Preflight diagnostics do not modify the checkout. Common failures:
unauthenticated Renovate or `gh`, a relevant dirty file, an unsafe chart
path, an uncovered image convention, invalid chart-local configuration.
Correct the named condition and rerun.
`doctor --for 'chart upgrade'` reports the tooling and credential half
without starting an upgrade.
