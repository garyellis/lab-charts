#!/usr/bin/env bash
set -euo pipefail

# Offline contract for the trivy-operator chart: the load-bearing statics that
# no cluster is needed to check and that a base-values edit could silently
# break. The runtime scan-to-metrics proof lives in the helm test
# (templates/tests/scan-to-metrics.yaml); this asserts what `helm template`
# alone can prove:
#   - the ServiceMonitor Alloy discovers (release: alloy), scrapes (port
#     metrics), and the cardinality guards on it (High/Critical only, drop
#     last_modified_date);
#   - every workload the chart owns or seeds runs non-root, seccomp
#     RuntimeDefault, drops ALL caps and cannot escalate (operator, server,
#     the operator-issued scan jobs, and the helm-test workloads);
#   - images are pinned to a release tag; and
#   - compliance.specs: [] renders no ClusterComplianceReport.
chart_dir="${1:-charts/trivy-operator}"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

fail() {
  echo "render-contract: $*" >&2
  exit 1
}
# q JQ_EXPR: jq over the rendered documents; fail on false/null.
q() {
  yq -o=json -I=0 '.' "${work}/ci.yaml" | jq -e -s "map(select(. != null)) | $1" >/dev/null
}

helm template trivy-operator "${chart_dir}" --namespace trivy-system \
  -f "${chart_dir}/values-ci.yaml" >"${work}/ci.yaml"

# --- ServiceMonitor: discovery label, scrape port, cardinality guards ------
sm='.[] | select(.kind == "ServiceMonitor")'
q "[${sm}] | length == 1" || fail "expected exactly one ServiceMonitor"
q "${sm} | .metadata.labels.release == \"alloy\"" ||
  fail "ServiceMonitor is missing the repo-wide 'release: alloy' discovery label"
q "${sm} | .spec.endpoints[0].port == \"metrics\"" ||
  fail "ServiceMonitor endpoint no longer scrapes the 'metrics' port"
q "${sm} | .spec.endpoints[].metricRelabelings | any(
    .action == \"drop\" and (.sourceLabels // []) == [\"__name__\", \"severity\"] and
    .regex == \"trivy_vulnerability_id;(Low|Medium|Unknown)\")" ||
  fail "the High/Critical-only drop for trivy_vulnerability_id is gone (per-CVE cardinality guard)"
q "${sm} | .spec.endpoints[].metricRelabelings | any(
    .action == \"labeldrop\" and .regex == \"last_modified_date\")" ||
  fail "the last_modified_date labeldrop is gone (per-CVE churn guard)"

# --- Scan jobs (operator-issued, configured via the operator ConfigMap) ----
pod_sc='.[] | select(.kind == "ConfigMap") | .data["scanJob.podTemplatePodSecurityContext"]
  | select(. != null) | fromjson'
ctr_sc='.[] | select(.kind == "ConfigMap") | .data["scanJob.podTemplateContainerSecurityContext"]
  | select(. != null) | fromjson'
q "[${pod_sc}] | length == 1 and (.[0] |
    .runAsNonRoot == true and .runAsUser == 65534 and .seccompProfile.type == \"RuntimeDefault\")" ||
  fail "scan-job pod securityContext is not non-root/seccomp RuntimeDefault"
q "[${ctr_sc}] | length == 1 and (.[0] |
    .allowPrivilegeEscalation == false and .privileged == false and
    .readOnlyRootFilesystem == true and .capabilities.drop == [\"ALL\"])" ||
  fail "scan-job container securityContext relaxes the restricted baseline"

# --- Every rendered pod: non-root, seccomp, drop ALL, no privilege escalation
# Covers the operator Deployment, trivy-server StatefulSet, and the helm-test
# ReplicaSet/Pod. Their pod specs are the .spec.template.spec / .spec of Pods.
specs='[.[] | select(.kind == "Deployment" or .kind == "StatefulSet" or .kind == "ReplicaSet")
    | .spec.template.spec]
  + [.[] | select(.kind == "Pod") | .spec]'
q "${specs} | length >= 4" ||
  fail "expected the operator, server, and both helm-test pods to render"
q "${specs} | all(.securityContext |
    .runAsNonRoot == true and .seccompProfile.type == \"RuntimeDefault\")" ||
  fail "a pod securityContext is not non-root/seccomp RuntimeDefault"
q "${specs} | all((.containers + (.initContainers // []))[] | .securityContext |
    .allowPrivilegeEscalation == false and .capabilities.drop == [\"ALL\"])" ||
  fail "a container may escalate privileges or does not drop ALL capabilities"

# --- Pinned images --------------------------------------------------------
yq -o=json -I=0 '.' "${work}/ci.yaml" |
  jq -r -s '[.. | objects | select(has("image")) | .image] | unique[]' |
  while IFS= read -r image; do
    [[ "${image}" =~ :v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "unpinned image ${image}"
  done

# --- Compliance off: no ClusterComplianceReport churn --------------------
q '[.[] | select(.kind == "ClusterComplianceReport")] | length == 0' ||
  fail "compliance.specs: [] should render no ClusterComplianceReport"

echo "render-contract: ok"
