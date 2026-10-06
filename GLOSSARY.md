# chart-manager

The CLI that renders, validates, tests, publishes, and promotes the Helm charts in `charts/`. Package
layout is set by [ADR-0001](docs/adr/0001-feature-packages.md).

## Language

### Code structure

**Command package**:
A package that owns one thing a user does with chart-manager, from its command to its result,
named after the CLI subcommand.
_Avoid_: feature module, service

**Leaf command**:
A command package that imports no other command package.
_Avoid_: scalar command, base command

**Composite command**:
A command package that answers a question about several leaf commands by asking each one through
its package interface, such as `plan` and `doctor`. It never imports another composite.
_Avoid_: aggregator, meta command

**Shared package**:
A package for one capability used by at least two command packages.
_Avoid_: common, utils, core, shared service

**Floor**:
The shared package every other package may use and that imports nothing else from `shared/`:
`workspace`. Inside `shared/`, `charts` and `events` are leaves over the floor and `cluster` is
the composite over them.
_Avoid_: base, core, common

**Integration**:
An adapter for one system outside chart-manager (a CLI tool, a cloud service, or the
Kubernetes cluster) that speaks only that system's language, answers questions about it and
makes no decisions of its own. One system may need several modules; two systems never share one.
_Avoid_: client wrapper, tool, port, shared integration

**Service**:
Only a Kubernetes `Service` (or Istio `VirtualService`). Not a name for packages or classes.

### Charts and clusters

**Chart**:
A chart directory with a `Chart.yaml` and an optional `chart-lifecycle.yaml`, whose names
agree.
_Avoid_: chart tree, chart dir

**Catalog**:
What `chart list` and `chart show` report: each chart's Helm metadata and lifecycle status.
_Avoid_: inventory, chart catalog service

**Dev cluster**:
The long-lived kind cluster a person develops charts against, managed by `local up`, `down`,
`reset` and `status`.
_Avoid_: development cluster, lab, sandbox, local cluster

**Test cluster**:
A throwaway kind cluster that `chart test` creates for one run and tears down afterwards.
_Avoid_: ephemeral cluster, sandbox

**Session**:
A provisioned or attached cluster together with the helm and kubectl bound to it, fixed for
the rest of the run.
_Avoid_: environment handle, rebinding clients

**Bootstrap**:
The charts a cluster installs before anything else, as declared for that cluster.
_Avoid_: prerequisites, base install

**Release converge**:
Installing or upgrading one release on a cluster, waiting for the workloads and CRDs in its
manifest to be ready, and reporting why it failed if it did not.
_Avoid_: deploy, install loop

### Validating a chart

**Validation check**:
Render, schema or policy, run on one chart in one environment by `chart validate`.
_Avoid_: phase, validator, gate

**Row**:
One chart in one environment in a validate run, with the result of each validation check.
_Avoid_: worklist row, target

### Testing a chart

**Chart test**:
The whole lifecycle `chart test` runs for a chart on a test cluster: install what it needs,
install the chart, run its checks, clean up. Configured by `spec.chartTest` in
`chart-lifecycle.yaml`.
_Avoid_: cluster test

**Helm test step**:
The single step in a chart test that runs `helm test`. `helmTest` is its name in
`chart-lifecycle.yaml` only.
_Avoid_: using "helm test" for the whole chart test

### Upgrading a chart

**Wrapper chart**:
A chart in `charts/` that pins upstream charts and images and has its own version and
`changelog.md`.

**Renovate**:
The dependency-update tool `chart upgrade` runs against one wrapper chart.

**Upgrade**:
`chart upgrade`: Renovate proposes one wrapper chart's dependency and image updates as one pull
request on a `renovate/<chart>/` branch.
_Avoid_: using "upgrade" for a Helm release (that is release converge)

**Finalize**:
`upgrade-finalize`, the hidden callback Renovate runs on the upgrade branch: it bumps the
wrapper chart's version (major or patch) and writes its changelog entry.
_Avoid_: finalizer

### Promoting a chart

**Environment**:
A Kubernetes cluster managed through a separate GitOps repo, where Flux runs a chart as a
HelmRelease.
_Avoid_: stage, target

**Promotion**:
Moving a chart version into an environment, in three stages: the promotion PR, monitoring the
HelmRelease until Flux reconciles it, and the promotion test.
_Avoid_: rollout, deploy, helmrelease (as the name of the flow)

**Promotion PR**:
The pull request to the GitOps repo that bumps a HelmRelease's chart version.

**Promotion test**:
Running `helm test` against a promoted release in its environment.
_Avoid_: chart test (that runs on a test cluster)

### Changing the code

**Behaviour check**:
What a rebuild must keep passing: the integration tests and CI. Names and shapes (CLI flags,
`-o json` output, exit codes, `api/` YAML) may change if every caller in this repo changes in
the same PR.
_Avoid_: fixed contract, public API, frozen API
