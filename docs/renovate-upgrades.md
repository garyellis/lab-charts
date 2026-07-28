# Renovate-driven chart upgrades

`chart-manager upgrade` asks Renovate to discover dependency updates for one
wrapper chart and turns the completed update into one repeatable pull request.

```bash
uv run chart-manager upgrade --path charts/cert-manager
uv run chart-manager upgrade --path charts/cert-manager --dry-run --format json
```

The path is resolved inside the current repository. Renovate performs the
platform checkout and does not switch branches in, stage files from, or commit
changes to the caller's checkout. Before starting, chart-manager verifies the
selected chart and its Renovate inputs have no uncommitted changes. Unrelated
local files do not become part of the upgrade.

## Authentication

Renovate uses its token for the platform checkout, branch, and pull request.
The GitHub CLI is used only to report whether the stable chart branch already
has an open pull request. Authenticate both before running:

```bash
git ls-remote origin HEAD
gh auth status
```

Renovate also needs credentials for GitHub and for every private package or
container registry referenced by the chart. Supply those through Renovate's
documented environment variables or the runner's secret store; never put
tokens in chart configuration or command arguments.

## Configuration and dependency coverage

Renovate resolves configuration in this order:

1. trusted self-hosted policy from `renovate-global.json`;
2. optional chart-local policy from `<chart>/renovate.json`;
3. chart-manager's generated request overlay, including the selected path,
   grouping, and callback;
4. the repository's root `renovate.json`, which Renovate loads once after
   cloning.

The global policy has a deliberately non-standard filename so Renovate does
not auto-load it a second time. Chart-local settings refine policy rather than
creating another Renovate entry point. The root configuration is trusted and
must not weaken the generated chart scope or callback restrictions.

Every mutable image used by a chart must be visible to Renovate. Prefer the
repository's conventional image repository/tag fields. For non-standard,
nested, or templated values, add the repository's Renovate coverage annotation
next to the value so the dependency has an explicit data source and package
name. The repository configuration also recognizes literal `image:
repository/name:tag` declarations in chart YAML and templates. Extraction
tests should be extended whenever a new image-value convention is introduced.

## Version and pull-request policy

The wrapper chart version is deterministic:

- bump the wrapper **major** version when any container image has a major
  update or any chart dependency has a major update;
- otherwise bump the wrapper **patch** version.

Each chart owns a branch namespace, `renovate/<chart>/`, and holds one branch
inside it. Re-running the same proposal finds and reports the existing open pull
request instead of creating a duplicate. If there is nothing to update, the
outcome is reported without a branch or pull request.

The per-chart namespace is what lets charts upgrade independently. Renovate's
stale-branch pruning is scoped by `branchPrefix` alone, but a chart-scoped run
extracts only one chart, so under a shared `renovate/` prefix every run would
look like the complete truth for the whole namespace and autoclose every other
chart's open pull request. Scoping the prefix makes the pruning scope and the
extraction scope agree: pruning stays enabled and reaches only the chart being
upgraded. `branchPrefix`, `branchPrefixOld`, and `includePaths` are set inside
`force` so the repository's own `renovate.json`, which Renovate merges last,
cannot re-break that isolation.

The branch name is never re-derived locally. Renovate owns the segment after the
prefix, so pull requests are matched by namespace prefix and the reported branch
comes from the pull request's head ref. More than one open pull request under a
chart's namespace is reported as a diagnostic rather than silently resolved.

A newer version arriving while a pull request is still open updates that pull
request in place; it does not open a second one. If someone has committed to the
branch, Renovate detects the modification and layers its update on top instead of
force-pushing over the work. Customization is still better placed in an ordinary
pull request against the chart's committed files: upgrade branches are
machine-owned and may be rebased or pruned.

`--dry-run` performs Renovate discovery without pushing a branch or opening a
pull request. The wrapper target version is finalized only when Renovate creates
an update branch, so it is absent from dry-run output and from runs that find
nothing to update. Otherwise it is read back from `Chart.yaml` on the pushed
branch through the GitHub API, which reports the artifact that exists rather
than predicting it locally, and leaves the checkout untouched.

That read-back is also the only check that the finalize callback ran. Renovate
records a failed or disallowed post-upgrade command as an artifact error, then
opens the pull request and exits zero, so a wrapper version still matching the
baseline is reported as a diagnostic. Renovate independently marks such a branch
with a warning in the pull-request body and a failing artifact-error status
check.

`--format json` keeps stdout stable for automation and includes the chart, path,
current and proposed wrapper versions, branch, outcome, pull-request data, and
diagnostics.

## Renovate callback

`chart-manager upgrade-finalize` is an internal, hidden callback used after
Renovate writes its result data. It is not an operator command. The callback
accepts the chart path plus the Renovate data file selected by the configured
callback environment.

Treat callback execution as privileged: run only repository-owned,
reviewed configuration; pin the Renovate runtime; restrict the token to the
target repository; validate the callback data path; and never execute callback
text obtained from dependency metadata. Pull-request creation remains
idempotent if Renovate or the job runner retries the callback.

## Troubleshooting

Preflight diagnostics are actionable and do not modify the caller's checkout.
The common failures are unauthenticated Renovate/`gh`, a relevant dirty file,
an unsafe chart path, an uncovered image convention, and invalid chart-local
configuration. Correct the named condition and rerun the same command.
