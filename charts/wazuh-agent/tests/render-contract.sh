#!/usr/bin/env bash
set -euo pipefail

# Offline contract for the wazuh-agent chart: enrollment secret hygiene,
# native image configuration, and the host-access boundary that the
# lifecycle policy exemption (root agent) would otherwise leave unchecked.
chart_dir="${1:-charts/wazuh-agent}"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

render() {
  local out="$1"
  shift
  helm template wazuh-agent "${chart_dir}" --namespace wazuh "$@" >"${out}"
}
fail() {
  echo "render-contract: $*" >&2
  exit 1
}
must_not_render() {
  local why="$1"
  shift
  if helm template wazuh-agent "${chart_dir}" --namespace wazuh "$@" >/dev/null 2>&1; then
    fail "${why}"
  fi
}
# q FILE JQ_EXPR: jq over the rendered documents; fail on false/null.
q() {
  yq -o=json -I=0 '.' "$1" | jq -e -s "map(select(. != null)) | $2" >/dev/null
}

render "${work}/ci.yaml" -f "${chart_dir}/values-ci.yaml"
ds='.[] | select(.kind == "DaemonSet") | .spec.template.spec'
agent="${ds} | .containers[0]"

# --- Enrollment secret -------------------------------------------------------
must_not_render "rendered without enrollment.existingSecret"
if cat "${chart_dir}"/templates/*.yaml "${chart_dir}"/templates/*.tpl "${chart_dir}"/templates/tests/*.yaml |
  grep -oE '\{\{[^}]*\}\}' | grep -E '\b(lookup|randAlpha|randAlphaNum|randNumeric|randAscii|randBytes|uuidv4)\b'; then
  fail "a template action uses lookup or random generation"
fi
q "${work}/ci.yaml" '[.[] | select(.kind == "Secret")] | length == 0' || fail "the chart renders a Secret"
q "${work}/ci.yaml" "${agent} | .env | any(.name == \"WAZUH_REGISTRATION_PASSWORD\" and
  .valueFrom.secretKeyRef == {\"name\": \"wazuh-credentials\", \"key\": \"authd-password\"})" ||
  fail "enrollment password is not read from the referenced Secret"
q "${work}/ci.yaml" '.[] | select(.metadata.name == "wazuh-agent-test-agent-active") | .spec.containers[0].env |
  any(.name == "API_PASSWORD" and .valueFrom.secretKeyRef == {"name": "wazuh-credentials", "key": "api-password"})' ||
  fail "helm test API password does not default to the enrollment Secret"

# --- Native image configuration ---------------------------------------------
q "${work}/ci.yaml" "${agent} | .env | (map(.name) | sort) ==
  [\"WAZUH_AGENT_GROUP\",\"WAZUH_AGENT_NAME\",\"WAZUH_MANAGER_PORT\",\"WAZUH_MANAGER_SERVER\",\"WAZUH_REGISTRATION_PASSWORD\",\"WAZUH_REGISTRATION_PORT\",\"WAZUH_REGISTRATION_SERVER\"]" ||
  fail "unexpected agent environment"
q "${work}/ci.yaml" "${agent} | .env | map({(.name): (.value // .valueFrom)}) | add |
  .WAZUH_MANAGER_SERVER == \"wazuh-manager-events.wazuh.svc.cluster.local\" and .WAZUH_MANAGER_PORT == \"1514\" and
  .WAZUH_REGISTRATION_SERVER == \"wazuh-manager-registration.wazuh.svc.cluster.local\" and .WAZUH_REGISTRATION_PORT == \"1515\" and
  .WAZUH_AGENT_NAME == {\"fieldRef\": {\"fieldPath\": \"spec.nodeName\"}} and .WAZUH_AGENT_GROUP == \"default\"" ||
  fail "manager addressing or node identity is wrong"
q "${work}/ci.yaml" '.[] | select(.kind == "ConfigMap") | .data["ossec.conf"] |
  test("<address>CHANGE_MANAGER_IP</address>") and test("<manager_address>CHANGE_ENROLL_IP</manager_address>") and
  test("<agent_name>CHANGE_AGENT_NAME</agent_name>") and test("<groups>CHANGE_AGENT_GROUP</groups>") and
  test("<directories check_all=\"yes\" realtime=\"yes\">/host/etc</directories>") and
  test("<location>/host/var/log/auth.log</location>") and
  test("<log_format>json</log_format>\\s*<location>/host/var/log/falco/events.json</location>")' ||
  fail "ossec.conf does not use the image placeholders or the values-driven FIM/log entries"
must_not_render "an FIM directory outside the mounted host paths rendered" \
  -f "${chart_dir}/values-ci.yaml" --set 'fim.directories[0].path=/host/usr/bin' --set 'fim.directories[0].realtime=false'
must_not_render "a log location outside the mounted host paths rendered" \
  -f "${chart_dir}/values-ci.yaml" --set 'logs[0].location=/host/opt/app.log' --set 'logs[0].format=syslog'
must_not_render "an empty manager address rendered" -f "${chart_dir}/values-ci.yaml" --set manager.events.address=
must_not_render "mounting the host root rendered" -f "${chart_dir}/values-ci.yaml" --set 'host.paths={/}'
must_not_render "mounting the container runtime socket rendered" -f "${chart_dir}/values-ci.yaml" --set 'host.paths={/run/containerd}'

# Identity persistence: restore before the agent starts (entrypoint script),
# supervised copy-back (s6 service), both reading the node state directory.
q "${work}/ci.yaml" "
  (${agent} | .volumeMounts | map(select(.name == \"config\")) | map({(.mountPath): .subPath}) | add) ==
    {\"/wazuh-config-mount/etc/ossec.conf\": \"ossec.conf\",
     \"/entrypoint-scripts/10-restore-identity.sh\": \"10-restore-identity.sh\",
     \"/etc/services.d/identity-sync/run\": \"identity-sync.sh\"} and
  (.[] | select(.kind == \"ConfigMap\") | .data |
    (.[\"10-restore-identity.sh\"] | test(\"/agent-state/client.keys|state}/client.keys\")) and
    (.[\"identity-sync.sh\"] | test(\"install -m 0600 -o root -g root\")))" ||
  fail "identity restore/sync wiring is wrong"

# --- Host-access boundary (what the policy exemption would hide) -------------
q "${work}/ci.yaml" "${ds} | (.hostPID // false) == false and (.hostNetwork // false) == false and (.hostIPC // false) == false" ||
  fail "the agent uses host namespaces"
q "${work}/ci.yaml" "${agent} | .securityContext |
  (.privileged // false) == false and .allowPrivilegeEscalation == false and
  .capabilities.drop == [\"ALL\"] and
  .capabilities.add == [\"CHOWN\",\"DAC_OVERRIDE\",\"FOWNER\",\"SETGID\",\"SETUID\",\"KILL\"] and
  .seccompProfile.type == \"RuntimeDefault\"" ||
  fail "agent securityContext exceeds the reviewed capability set"
q "${work}/ci.yaml" "
  (${ds} | .volumes | map(select(.hostPath)) | map({(.name): .hostPath}) | add) as \$hp |
  (${agent} | .volumeMounts | map(select(\$hp[.name] != null))) as \$mounts |
  (\$mounts | map(select(.readOnly != true)) | map(.name)) == [\"state\"] and
  \$hp.state == {\"path\": \"/var/lib/wazuh-agent\", \"type\": \"DirectoryOrCreate\"} and
  (\$mounts | map(select(.name != \"state\") | .mountPath) | sort) == [\"/host/etc\", \"/host/var/log\"]" ||
  fail "host paths must be read-only under /host, with only the identity directory writable"
q "${work}/ci.yaml" "${ds} | .tolerations | any(.key == \"node-role.kubernetes.io/control-plane\" and .operator == \"Exists\")" ||
  fail "control-plane nodes are not tolerated"
q "${work}/ci.yaml" "${ds} | .automountServiceAccountToken == false" || fail "the agent mounts a service account token"
q "${work}/ci.yaml" '[.[] | select(.kind == "Service" or .kind == "Role" or .kind == "ClusterRole")] | length == 0' ||
  fail "the chart renders Services or RBAC"
q "${work}/ci.yaml" '.[] | select(.metadata.name == "wazuh-agent-test-agent-active") |
  .spec.securityContext.runAsNonRoot == true and .spec.containers[0].securityContext.runAsNonRoot == true' ||
  fail "the helm test does not run as non-root"
q "${work}/ci.yaml" '.[] | select(.kind == "DaemonSet") | .spec.updateStrategy == {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}} and
  (.spec.template.metadata.annotations | has("checksum/config"))' ||
  fail "update strategy or config checksum is wrong"
yq -o=json -I=0 '.' "${work}/ci.yaml" | jq -r -s '[.. | objects | select(has("image")) | .image] | unique[]' |
  while IFS= read -r image; do
    [[ "${image}" =~ :[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "unpinned image ${image}"
  done

echo "render-contract: ok"
