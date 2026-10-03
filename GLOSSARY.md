# chart-manager

The CLI that renders, validates, tests, publishes, and promotes the Helm charts in `charts/`. Package
layout is set by [ADR-0001](docs/adr/0001-feature-packages.md).

## Language

**Command package**:
A package that owns one thing a user does with chart-manager, from its command to its result.
_Avoid_: feature module, service

**Shared package**:
A package for one capability used by at least two command packages.
_Avoid_: common, utils, core

**Chart**:
A chart directory with a `Chart.yaml` and an optional `chart-lifecycle.yaml`, whose names
agree.
_Avoid_: chart tree, chart dir

**Release converge**:
Installing or upgrading one release on a cluster, waiting for it to be ready, and reporting
why it failed if it did not.
_Avoid_: deploy, install loop

**Dev cluster**:
The long-lived kind cluster a person develops charts against, managed by `local up`, `down`,
`reset` and `status`.
_Avoid_: development cluster, lab, sandbox, local cluster

**Test cluster**:
A throwaway kind cluster that `chart test` creates for one run and tears down afterwards.
_Avoid_: ephemeral cluster, sandbox

**Chart test**:
The whole lifecycle `chart test` runs for a chart on a test cluster: install what it needs,
install the chart, run its checks, clean up. Configured today by `spec.clusterTest` in
`chart-lifecycle.yaml`; rename the key to match when `chart_test/` is rebuilt.
_Avoid_: cluster test

**Helm test step**:
The single step in a chart test that runs `helm test`. `helmTest` is its name in
`chart-lifecycle.yaml` only.
_Avoid_: using "helm test" for the whole chart test

**Behaviour check**:
What a rebuild must keep passing: the integration tests and CI. Names and shapes (CLI flags,
`-o json` output, exit codes, `api/` YAML) may change if every caller in this repo changes in
the same PR.
_Avoid_: fixed contract, public API, frozen API
