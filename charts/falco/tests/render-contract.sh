#!/usr/bin/env bash
set -euo pipefail

# Offline contract for the falco chart: the privilege boundary that the
# lifecycle policy exemption (root DaemonSet) would otherwise leave unchecked,
# and the alert file that charts/wazuh-agent reads.
chart_dir="${1:-charts/falco}"
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

helm template falco "${chart_dir}" --namespace falco -f "${chart_dir}/values-ci.yaml" >"${work}/ci.yaml"
ds='.[] | select(.kind == "DaemonSet") | .spec.template.spec'

q "${ds} | (.hostPID // false) == false and (.hostNetwork // false) == false and (.hostIPC // false) == false" ||
  fail "falco uses host namespaces"
q "${ds} | .containers | length == 1 and (.[0].securityContext |
  (.privileged // false) == false and .allowPrivilegeEscalation == false and
  .seccompProfile.type == \"RuntimeDefault\" and
  (.capabilities.add | sort) == [\"BPF\",\"PERFMON\",\"SYS_PTRACE\",\"SYS_RESOURCE\"])" ||
  fail "falco securityContext exceeds the reviewed capability set or drops APE/seccomp hardening"
q "${ds} | (.initContainers // []) | length == 0" ||
  fail "an init container rendered (driver loader or falcoctl download)"
q "
  (${ds} | .volumes | map(select(.hostPath)) | map({(.name): .hostPath.path}) | add) as \$hp |
  (${ds} | .containers[0].volumeMounts | map(select(\$hp[.name] != null and .readOnly != true)) |
    map(\$hp[.name]) | sort) == [\"/proc\", \"/run/containerd\", \"/var/log/falco\"]" ||
  fail "writable host paths must be /proc, the CRI socket directory and the alert directory only"
q '.[] | select(.kind == "ConfigMap") | .data["falco.yaml"] | test("json_output: true") and
  test("filename: /var/log/falco/events.json")' ||
  fail "alerts are not written as JSON to /var/log/falco/events.json"
q '[.[] | select(.kind == "Service" or .kind == "Secret")] | length == 0' || fail "the chart renders a Service or Secret"
yq -o=json -I=0 '.' "${work}/ci.yaml" | jq -r -s '[.. | objects | select(has("image")) | .image] | unique[]' |
  while IFS= read -r image; do
    [[ "${image}" =~ :[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "unpinned image ${image}"
  done

echo "render-contract: ok"
