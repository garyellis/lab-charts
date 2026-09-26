#!/usr/bin/env bash
set -euo pipefail

# Offline contract for the reloader chart: the invariants a values edit can
# silently break and `helm lint` will not catch -- the digest pin (the tag is
# cosmetic; the digest is what runs), the annotations reload strategy (so
# Helm/Flux/Argo see no drift), the hardened container, a single replica, and
# no prometheus-operator dependency by default.
chart_dir="${1:-charts/reloader}"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

fail() {
  echo "render-contract: $*" >&2
  exit 1
}
# q JQ_EXPR: jq over the rendered documents; fail on false/null.
q() {
  yq -o=json -I=0 '.' "${work}/default.yaml" | jq -e -s "map(select(. != null)) | $1" >/dev/null
}

# Default values only: "by default" must hold without the CI overlay.
helm template reloader "${chart_dir}" --namespace reloader >"${work}/default.yaml"

# The reloader Deployment, excluding the chart's helm-test hook Deployments.
dep='.[] | select(.kind == "Deployment" and ((.metadata.annotations["helm.sh/hook"]) // "") == "")'
spec="${dep} | .spec.template.spec"
ctr="${spec} | .containers[0]"

q "${ctr} | .image | test(\"@sha256:[0-9a-f]{64}$\")" ||
  fail "reloader image is not digest-pinned (tag alone would drift from the running image)"
q "${ctr} | (.args // []) | any(. == \"--reload-strategy=annotations\")" ||
  fail "reload strategy is not annotations (env-var injection would show as GitOps drift)"
q "${spec} | .securityContext | (.runAsNonRoot == true) and (.seccompProfile.type == \"RuntimeDefault\")" ||
  fail "pod securityContext is not non-root with RuntimeDefault seccomp"
q "${ctr} | .securityContext | (.readOnlyRootFilesystem == true) and (.allowPrivilegeEscalation == false) and
  (.capabilities.drop == [\"ALL\"]) and (.seccompProfile.type == \"RuntimeDefault\")" ||
  fail "container securityContext is not hardened (readOnlyRootFilesystem / allowPrivilegeEscalation / drop ALL / seccomp)"
q "[${dep}] | length == 1 and (.[0].spec.replicas == 1)" ||
  fail "expected exactly one reloader Deployment with replicas == 1"
q '[.[] | select(.kind == "ServiceMonitor" or .kind == "PodMonitor")] | length == 0' ||
  fail "a ServiceMonitor or PodMonitor rendered by default (would require prometheus-operator CRDs)"

echo "render-contract: ok"
