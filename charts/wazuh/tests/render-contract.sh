#!/usr/bin/env bash
set -euo pipefail

# Offline contract for the wazuh chart: credential hygiene, release-scoped
# names, the policy intent the lifecycle exemption does not cover, and the
# Istio exposure shape. Lifecycle validation separately checks schemas.
chart_dir="${1:-charts/wazuh}"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

render() {
  local out="$1"
  shift
  helm template wazuh "${chart_dir}" --namespace security "$@" >"${out}"
}

fail() {
  echo "render-contract: $*" >&2
  exit 1
}

render "${work}/ci.yaml" -f "${chart_dir}/values-ci.yaml"
render "${work}/owned-gw.yaml" -f "${chart_dir}/values-ci.yaml" \
  --set istio.gateway.create=true --set istio.gateway.tls.credentialName=apps-wildcard-cert
render "${work}/no-istio.yaml" -f "${chart_dir}/values-ci.yaml" --set istio.enabled=false
render "${work}/sso.yaml" -f "${chart_dir}/values-ci.yaml" -f "${chart_dir}/values-ci-sso.yaml"
render "${work}/renamed.yaml" -f "${chart_dir}/values-ci.yaml" --set fullnameOverride=siem-a
render "${work}/workerless.yaml" -f "${chart_dir}/values-ci.yaml" --set manager.workers.replicas=0

# --- Credentials -----------------------------------------------------------

# 1. Rendering fails without a credentials source, and an incomplete CI
#    opt-in fails too (no defaults, no generation).
if helm template wazuh "${chart_dir}" --namespace security >/dev/null 2>&1; then
  fail "rendered without credentials.existingSecret or credentials.create"
fi
if helm template wazuh "${chart_dir}" --namespace security --set credentials.create=true >/dev/null 2>&1; then
  fail "credentials.create=true rendered without explicit values"
fi

# 2. No lookup, randomness or in-template hashing in any template action.
if cat "${chart_dir}"/templates/*.tpl "${chart_dir}"/templates/*.yaml "${chart_dir}"/templates/*/* |
  grep -oE '\{\{[^}]*\}\}' |
  grep -E '\b(lookup|randAlpha|randAlphaNum|randNumeric|randAscii|randBytes|genPrivateKey|bcrypt|htpasswd|uuidv4)\b'; then
  fail "a template action uses lookup, random generation or hashing"
fi

# 3. The default values carry no secret material.
yq -o=json '.credentials.values' "${chart_dir}/values.yaml" | jq -e 'to_entries | all(.value == "")' >/dev/null ||
  fail "values.yaml ships a default credential"

# q FILE JQ_EXPR: evaluate a jq expression over the rendered documents
# (slurped into one array, nulls dropped) and fail on false/null.
q() {
  local file="$1" expr="$2"
  yq -o=json -I=0 '.' "${file}" | jq -e -s "map(select(. != null)) | ${expr}" >/dev/null
}
docs() {
  yq -o=json -I=0 '.' "$1" | jq -c -s 'map(select(. != null))'
}

# 4. No password hash or known upstream demo password appears in any render,
#    no demo user exists, and the only Secret is the explicit CI opt-in.
for file in "${work}"/*.yaml; do
  if grep -En '\$2[aby]\$[0-9]{2}\$' "${file}"; then
    fail "$(basename "${file}") renders a bcrypt hash"
  fi
  if grep -En 'MyS3cr37P450r|SecretPassword|kibanaserver:kibanaserver|wazuh-wui:wazuh-wui' "${file}"; then
    fail "$(basename "${file}") renders an upstream default password"
  fi
  if grep -En '\b(kibanaro|logstash|anomalyadmin|snapshotrestore)\b' "${file}"; then
    fail "$(basename "${file}") renders a demo user"
  fi
done
q "${work}/sso.yaml" '[.[] | select(.kind == "Secret")] | length == 0' ||
  fail "a Secret is rendered when credentials.existingSecret is used"
q "${work}/ci.yaml" '[.[] | select(.kind == "Secret") | .metadata.name] == ["wazuh-credentials"]' ||
  fail "the CI credentials Secret is missing or not release-scoped"

# 5. internal_users.yml is rendered at runtime from the Secret via hash.sh -env,
#    in both the indexer pods and the security-config Deployment, including
#    the least-privilege wazuh-writer user.
q "${work}/ci.yaml" '
  [.[] | select(.kind == "StatefulSet" and .metadata.name == "wazuh-indexer"),
         select(.kind == "Deployment" and .metadata.name == "wazuh-security-config") |
   .spec.template.spec.initContainers[] | select(.name == "render-security-config") |
   (.command[2] | test("tools/hash.sh")) and (.command[2] | test("-env INDEXER_ADMIN_PASSWORD")) and
   (.command[2] | test("-env INDEXER_WRITER_PASSWORD")) and (.command[2] | test("wazuh-writer:")) and
   ([.env[].valueFrom.secretKeyRef.name] | unique == ["wazuh-credentials"])] | length == 2 and all' ||
  fail "internal_users.yml is not rendered at runtime from the credentials Secret"
q "${work}/ci.yaml" '[.[] | select(.kind == "ConfigMap") | .data | has("internal_users.yml")] | any | not' ||
  fail "internal_users.yml is rendered into a ConfigMap"

# 6. The cluster key never lands in the ConfigMap; the image substitutes it.
q "${work}/ci.yaml" '
  .[] | select(.kind == "ConfigMap" and .metadata.name == "wazuh-manager-config") |
  (.data["master.conf"] | test("<key>to_be_replaced_by_cluster_key</key>")) and
  (.data["worker.conf"] | test("<node_name>to_be_replaced_by_hostname</node_name>"))' ||
  fail "ossec.conf does not use the runtime cluster-key and node-name placeholders"

# --- Release-scoped names --------------------------------------------------

# Every Secret and Certificate, and every Secret a pod references, carries the
# release fullname prefix. The one exception is credentials.existingSecret.
check_scoped() {
  local file="$1" prefix="$2" existing="${3:-}"
  docs "${file}" | jq -e --arg p "${prefix}-" --arg existing "${existing}" '
    [ (.[] | select(.kind == "Secret" or .kind == "Certificate") | .metadata.name),
      (.[] | select(.kind == "Certificate") | .spec.secretName),
      (.. | objects | select(has("secretName")) | .secretName),
      (.. | objects | select(has("secretKeyRef")) | .secretKeyRef.name) ]
    | unique | map(select(. != $existing)) | all(startswith($p))' >/dev/null ||
    fail "$(basename "${file}"): a Secret or Certificate name is not release-scoped (${prefix}-*)"
}
check_scoped "${work}/ci.yaml" wazuh
check_scoped "${work}/renamed.yaml" siem-a
render "${work}/prod.yaml" --set credentials.existingSecret=site-wazuh-credentials
check_scoped "${work}/prod.yaml" wazuh site-wazuh-credentials

# --- Policy intent not covered by the lifecycle exemption -------------------

# Every workload except the root-only manager runs every container non-root.
q "${work}/ci.yaml" '
  [.[] | select(.kind == "StatefulSet" or .kind == "Deployment" or .kind == "Job" or .kind == "Pod") |
   select(.metadata.labels["app.kubernetes.io/component"] != "manager") |
   (.spec.template.spec // .spec) as $pod |
   $pod.containers[] | (.securityContext.runAsNonRoot == true) or ($pod.securityContext.runAsNonRoot == true)] | all' ||
  fail "a non-manager container does not run as non-root"
q "${work}/ci.yaml" '[.[] | select(.kind == "Service" and .spec.type == "LoadBalancer")] | length == 0' ||
  fail "a LoadBalancer Service is rendered by default"
render "${work}/lb.yaml" -f "${chart_dir}/values-ci.yaml" \
  --set manager.agentServices.type=LoadBalancer \
  --set 'manager.agentServices.loadBalancerSourceRanges={10.0.0.0/8}'
q "${work}/lb.yaml" '
  [.[] | select(.kind == "Service" and .spec.type == "LoadBalancer")] |
  (map(.metadata.name) | sort) == ["wazuh-manager-events", "wazuh-manager-registration"] and
  all(.spec.loadBalancerSourceRanges == ["10.0.0.0/8"])' ||
  fail "LoadBalancer opt-in must expose exactly the two agent Services with source ranges"

# The privileged sysctl init container is opt-in only.
q "${work}/ci.yaml" '[.[] | select(.kind == "StatefulSet") | .spec.template.spec.initContainers[]? | select(.name == "sysctl")] | length == 0' ||
  fail "the privileged sysctl init container renders by default"
render "${work}/sysctl.yaml" -f "${chart_dir}/values-ci.yaml" --set indexer.sysctlInitContainer.enabled=true
q "${work}/sysctl.yaml" '[.[] | select(.kind == "StatefulSet" and .metadata.name == "wazuh-indexer") | .spec.template.spec.initContainers[0] | .name == "sysctl" and .securityContext.privileged == true] | all and length == 1' ||
  fail "sysctl init container opt-in is wrong"

# Pod checksums hash only what each workload reads at runtime; certificates
# and credentials roll through Reloader (secret.reloader.stakater.com/reload).
q "${work}/ci.yaml" '
  [.[] | select(.kind == "StatefulSet" or .kind == "Deployment") | .spec.template.metadata.annotations // {} |
   (has("checksum/certificates") | not) and (has("checksum/credentials") | not)] | all' ||
  fail "certificate/credential checksums must be replaced by Reloader when reloader.enabled"
q "${work}/ci.yaml" '
  .[] | select(.kind == "StatefulSet" and .metadata.name == "wazuh-indexer") | .spec.template.metadata.annotations | keys == ["checksum/config"]' ||
  fail "indexer pods must hash only opensearch.yml"
reload_lists='
  [.[] | select(.metadata.annotations["secret.reloader.stakater.com/reload"]) |
   {key: "\(.kind)/\(.metadata.name)", value: .metadata.annotations["secret.reloader.stakater.com/reload"]}] | from_entries'
q "${work}/ci.yaml" "${reload_lists} == {
  \"StatefulSet/wazuh-indexer\": \"wazuh-indexer-tls\",
  \"StatefulSet/wazuh-manager-master\": \"wazuh-manager-tls,wazuh-credentials\",
  \"StatefulSet/wazuh-manager-worker\": \"wazuh-manager-tls,wazuh-credentials\",
  \"Deployment/wazuh-dashboard\": \"wazuh-dashboard-tls,wazuh-credentials\",
  \"Deployment/wazuh-security-config\": \"wazuh-credentials,wazuh-admin-tls,wazuh-indexer-tls\"}" ||
  fail "Reloader annotations do not list the exact Secrets each workload reads"
q "${work}/sso.yaml" "${reload_lists} | .\"Deployment/wazuh-dashboard\" == \"wazuh-dashboard-tls,wazuh-credentials,wazuh-oidc\" and
  .\"StatefulSet/wazuh-manager-master\" == \"wazuh-manager-tls,wazuh-credentials\"" ||
  fail "Reloader annotations must name the existingSecret and the OIDC client Secret"
render "${work}/no-reloader.yaml" -f "${chart_dir}/values-ci.yaml" --set reloader.enabled=false
q "${work}/no-reloader.yaml" '
  ([.[] | select(.metadata.annotations["secret.reloader.stakater.com/reload"])] | length == 0) and
  ([.[] | select(.kind == "Deployment" and .metadata.name == "wazuh-dashboard") | .spec.template.metadata.annotations | has("checksum/credentials")] | all)' ||
  fail "reloader.enabled=false must drop the annotations and fall back to credential checksums"

# Every image is pinned to an explicit, non-latest version tag.
docs "${work}/ci.yaml" | jq -r '[.. | objects | select(has("image")) | .image] | unique[]' | while IFS= read -r image; do
  [[ "${image}" =~ :[0-9][A-Za-z0-9._-]*$ ]] || fail "unpinned image ${image}"
done

# --- Topology --------------------------------------------------------------

q "${work}/ci.yaml" '.[] | select(.kind == "ConfigMap" and .metadata.name == "wazuh-indexer-config") | .data["opensearch.yml"] | test("discovery.type: single-node")' ||
  fail "single-node discovery missing for one indexer replica"
q "${work}/prod.yaml" '.[] | select(.kind == "ConfigMap" and .metadata.name == "wazuh-indexer-config") | .data["opensearch.yml"] |
  test("cluster.initial_cluster_manager_nodes:\n  - wazuh-indexer-0\n  - wazuh-indexer-1\n  - wazuh-indexer-2") and (test("single-node") | not)' ||
  fail "multi-node bootstrap list is wrong"
q "${work}/prod.yaml" '[.[] | select(.kind == "PodDisruptionBudget") | .metadata.name] | sort == ["wazuh-indexer", "wazuh-manager-worker"]' ||
  fail "PodDisruptionBudgets for indexer and workers expected in the default topology"
q "${work}/ci.yaml" '[.[] | select(.kind == "PodDisruptionBudget")] | length == 0' ||
  fail "PodDisruptionBudgets must not render for single replicas"
q "${work}/workerless.yaml" '
  (.[] | select(.kind == "Service" and .metadata.name == "wazuh-manager-events") | .spec.selector["wazuh.com/node-type"] == "master") and
  ([.[] | select(.kind == "StatefulSet" and .metadata.name == "wazuh-manager-worker")] | length == 0)' ||
  fail "zero workers must drop the worker StatefulSet and route events to the master"
q "${work}/ci.yaml" '
  [.[] | select(.kind == "StatefulSet") | .metadata.name | length <= 52] | all' ||
  fail "a StatefulSet name exceeds 52 characters"
render "${work}/long.yaml" -f "${chart_dir}/values-ci.yaml" --set fullnameOverride=abcdefghijklmnopqrstuvwxyz0123456789
q "${work}/long.yaml" '[.[] | select(.kind == "StatefulSet") | .metadata.name | length <= 52] | all' ||
  fail "a StatefulSet name exceeds 52 characters with a maximal fullname"

# --- Istio -----------------------------------------------------------------

q "${work}/no-istio.yaml" '[.[] | select(.apiVersion == "networking.istio.io/v1")] | length == 0' ||
  fail "Istio resources render with istio.enabled=false"
q "${work}/no-istio.yaml" '[.[] | select(.metadata.name == "wazuh-test-dashboard-ingress")] | length == 0' ||
  fail "the ingress helm test renders with istio.enabled=false"

# Shared VirtualService/DestinationRule shape (both gateway modes).
vs_shape='
  .spec.hosts == ["wazuh.kind.local"] and
  .spec.http[0].route[0].destination.host == "wazuh-dashboard.security.svc.cluster.local" and
  .spec.http[0].route[0].destination.port.number == 5601 and
  .metadata.labels["app.kubernetes.io/instance"] == "wazuh"'

# Chart-owned Gateway mode (create=true): Gateway in the release namespace,
# mirroring apps-gateway (80 redirect + 443 SIMPLE), bound by the VirtualService.
q "${work}/owned-gw.yaml" "
  ([.[] | select(.kind == \"Gateway\")] | length == 1) and
  (.[] | select(.kind == \"Gateway\") |
    .apiVersion == \"networking.istio.io/v1\" and
    .metadata.name == \"wazuh\" and .metadata.namespace == \"security\" and
    .spec.selector == {\"istio\": \"gateway-internal\"} and
    (.spec.servers | length == 2) and
    (.spec.servers[0] | .port.number == 80 and .port.protocol == \"HTTP\" and .tls.httpsRedirect == true and .hosts == [\"wazuh.kind.local\"]) and
    (.spec.servers[1] | .port.number == 443 and .port.protocol == \"HTTPS\" and .port.name == \"https-wazuh\" and
      .tls.mode == \"SIMPLE\" and .tls.credentialName == \"apps-wildcard-cert\" and .hosts == [\"wazuh.kind.local\"])) and
  (.[] | select(.kind == \"VirtualService\") | .spec.gateways == [\"security/wazuh\"] and ${vs_shape})" ||
  fail "chart-owned Gateway mode is wrong"
if helm template wazuh "${chart_dir}" --namespace security -f "${chart_dir}/values-ci.yaml" \
  --set istio.gateway.create=true >/dev/null 2>&1; then
  fail "a chart-owned Gateway rendered without tls.credentialName"
fi
render "${work}/named-gw.yaml" -f "${chart_dir}/values-ci.yaml" --set istio.gateway.create=true --set istio.gateway.tls.credentialName=apps-wildcard-cert \
  --set istio.gateway.name=siem --set istio.gateway.namespace=edge
q "${work}/named-gw.yaml" '
  (.[] | select(.kind == "Gateway") | .metadata.name == "siem" and .metadata.namespace == "edge") and
  (.[] | select(.kind == "VirtualService") | .spec.gateways == ["edge/siem"])' ||
  fail "istio.gateway.name/namespace not honored"

# Shared gateway mode (values-ci.yaml): no Gateway rendered, VirtualService binds to `existing`.
q "${work}/ci.yaml" "
  ([.[] | select(.kind == \"Gateway\")] | length == 0) and
  (.[] | select(.kind == \"VirtualService\") | .spec.gateways == [\"istio-ingress/apps-gateway\"] and ${vs_shape})" ||
  fail "shared gateway mode is wrong"

q "${work}/ci.yaml" '
  .[] | select(.kind == "DestinationRule") | .spec.trafficPolicy.portLevelSettings[0] |
  .port.number == 5601 and .tls.mode == "SIMPLE" and
  .tls.sni == "wazuh-dashboard.security.svc.cluster.local" and .tls.insecureSkipVerify == true' ||
  fail "DestinationRule default TLS origination is wrong"
q "${work}/sso.yaml" '
  .[] | select(.kind == "DestinationRule") | .spec.trafficPolicy.portLevelSettings[0].tls |
  .credentialName == "wazuh-dashboard-ca" and (has("insecureSkipVerify") | not) and
  .subjectAltNames == ["wazuh-dashboard.security.svc.cluster.local"]' ||
  fail "DestinationRule verified-TLS mode is wrong"

# Custom hosts drive the Gateway, the VirtualService and the ingress test.
render "${work}/hosts.yaml" -f "${chart_dir}/values-ci.yaml" --set istio.gateway.create=true --set istio.gateway.tls.credentialName=apps-wildcard-cert --set 'istio.dashboard.hosts={siem.example.com}'
q "${work}/hosts.yaml" '
  (.[] | select(.kind == "VirtualService") | .spec.hosts == ["siem.example.com"]) and
  (.[] | select(.kind == "Gateway") | all(.spec.servers[]; .hosts == ["siem.example.com"])) and
  (.[] | select(.metadata.name == "wazuh-test-dashboard-ingress") | .spec.containers[0].command[4] | test("host=.siem.example.com."))' ||
  fail "istio.dashboard.hosts does not drive the Gateway, VirtualService and ingress test"
q "${work}/ci.yaml" '
  .[] | select(.metadata.name == "wazuh-test-dashboard-ingress") | .spec.containers[0].command[4] |
  test("host=.wazuh.kind.local.") and test("istio-gateway.istio-ingress.svc.cluster.local:443")' ||
  fail "ingress helm test does not target the ingress gateway Service"

# --- SSO -------------------------------------------------------------------

yaml_field() {
  docs "$1" | jq -r --arg name "$2" --arg key "$3" '.[] | select(.kind == "ConfigMap" and .metadata.name == $name) | .data[$key]' |
    yq -o=json -I=0 '.'
}
yaml_field "${work}/sso.yaml" wazuh-indexer-security config.yml | jq -e '
  .config.dynamic.authc |
  has("openid_auth_domain") and has("saml_auth_domain") and
  .basic_internal_auth_domain.http_authenticator.challenge == false and
  .openid_auth_domain.http_authenticator.config.roles_key == "groups" and
  .saml_auth_domain.http_authenticator.config.exchange_key == "${env.SAML_EXCHANGE_KEY}"' >/dev/null ||
  fail "SSO auth domains are wrong"
yaml_field "${work}/ci.yaml" wazuh-indexer-security config.yml | jq -e '
  .config.dynamic.authc | keys == ["basic_internal_auth_domain", "clientcert_auth_domain"] and .basic_internal_auth_domain.http_authenticator.challenge == false' >/dev/null ||
  fail "SSO auth domains render while SSO is disabled"
yaml_field "${work}/sso.yaml" wazuh-indexer-security roles_mapping.yml | jq -e '
  .all_access.backend_roles == ["admin", "wazuh-admins"] and .kibana_user.backend_roles == ["wazuh-users"] and
  .readall.backend_roles == ["wazuh-readers"] and .kibana_server.users == ["kibanaserver"]' >/dev/null ||
  fail "SSO role mappings are wrong"
yaml_field "${work}/sso.yaml" wazuh-dashboard-config opensearch_dashboards.yml | jq -e '
  .["opensearch_security.auth.type"] == ["openid", "saml", "basicauth"] and
  .["opensearch_security.openid.base_redirect_url"] == "https://wazuh.kind.local"' >/dev/null ||
  fail "dashboard SSO configuration is wrong"
q "${work}/sso.yaml" '
  (.[] | select(.kind == "StatefulSet" and .metadata.name == "wazuh-indexer") | .spec.template.spec.containers[0].env |
   any(.name == "SAML_EXCHANGE_KEY" and .valueFrom.secretKeyRef.name == "wazuh-saml")) and
  (.[] | select(.kind == "Deployment" and .metadata.name == "wazuh-dashboard") | .spec.template.spec.containers[0].env |
   any(.name == "OIDC_CLIENT_SECRET" and .valueFrom.secretKeyRef.name == "wazuh-oidc"))' ||
  fail "SSO secrets are not wired from their Secrets"
if helm template wazuh "${chart_dir}" --namespace security -f "${chart_dir}/values-ci.yaml" --set sso.oidc.enabled=true >/dev/null 2>&1; then
  fail "OIDC enabled without connectUrl/clientSecret rendered"
fi

# --- Review hardening --------------------------------------------------------

# No manager pod holds the indexer admin password; it uses wazuh-writer.
q "${work}/ci.yaml" '
  [.[] | select(.kind == "StatefulSet" and .metadata.labels["app.kubernetes.io/component"] == "manager") |
   .spec.template.spec.containers[0].env] |
  all(any(.name == "INDEXER_USERNAME" and .value == "wazuh-writer") and
      any(.name == "INDEXER_PASSWORD" and .valueFrom.secretKeyRef.key == "indexer-writer-password") and
      all(.valueFrom.secretKeyRef.key? != "indexer-admin-password"))' ||
  fail "manager pods must use wazuh-writer and never the admin password"
yaml_field "${work}/ci.yaml" wazuh-indexer-security roles.yml | jq -e '
  .wazuh_writer.index_permissions[0].index_patterns == ["wazuh-alerts-*","wazuh-archives-*","wazuh-states-*","wazuh-monitoring-*","wazuh-statistics-*"] and
  (.wazuh_writer.index_permissions[0].allowed_actions | index("indices_all") | not) and
  .wazuh_node_monitor.cluster_permissions == ["cluster_monitor"] and has("manage_wazuh_index")' >/dev/null ||
  fail "chart roles wazuh_writer / wazuh_node_monitor are wrong"
yaml_field "${work}/ci.yaml" wazuh-indexer-security roles_mapping.yml | jq -e '
  .wazuh_writer.users == ["wazuh-writer"] and .wazuh_node_monitor.users == ["wazuh-indexer"]' >/dev/null ||
  fail "chart role mappings are wrong"

# Readiness: node-cert exec probe on local cluster health, no password.
q "${work}/ci.yaml" '
  .[] | select(.kind == "StatefulSet" and .metadata.name == "wazuh-indexer") | .spec.template.spec.containers[0] |
  (.readinessProbe.exec.command | join(" ") |
    test("--cert /usr/share/wazuh-indexer/config/certs/tls.crt") and test("_cluster/health\\?local=true") and (test("-u |--user") | not)) and
  ([.env[].name] | index("INDEXER_ADMIN_PASSWORD") | not)' ||
  fail "indexer readiness must be a node-certificate cluster health check"
yaml_field "${work}/ci.yaml" wazuh-indexer-security config.yml | jq -e '
  .config.dynamic.authc.clientcert_auth_domain | .http_enabled == true and .transport_enabled == false and
  .http_authenticator.config.username_attribute == "cn"' >/dev/null ||
  fail "clientcert auth domain missing"

# Default anti-affinity for indexer and workers; user affinity replaces it.
q "${work}/ci.yaml" '
  [.[] | select(.kind == "StatefulSet" and (.metadata.name == "wazuh-indexer" or .metadata.name == "wazuh-manager-worker")) |
   .spec.template.spec.affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[0].podAffinityTerm.topologyKey] ==
  ["kubernetes.io/hostname", "kubernetes.io/hostname"]' ||
  fail "default anti-affinity missing"
render "${work}/affinity.yaml" -f "${chart_dir}/values-ci.yaml" --set-json 'indexer.affinity={"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"pool","operator":"In","values":["siem"]}]}]}}}'
q "${work}/affinity.yaml" '.[] | select(.kind == "StatefulSet" and .metadata.name == "wazuh-indexer") | .spec.template.spec.affinity | keys == ["nodeAffinity"]' ||
  fail "indexer.affinity does not replace the default"

# Scale-independent indexer certificate; DNs derived from certificates.subject.
q "${work}/ci.yaml" '
  .[] | select(.kind == "Certificate" and .metadata.name == "wazuh-indexer-tls") | .spec |
  (.dnsNames | index("*.wazuh-indexer-nodes.security.svc.cluster.local")) and
  ([.dnsNames[] | select(test("^wazuh-indexer-[0-9]"))] | length == 0) and
  .subject.organizationalUnits == ["wazuh.security"] and .subject.organizations == ["Wazuh"]' ||
  fail "indexer certificate SANs or subject are wrong"
render "${work}/scaled.yaml" --set credentials.existingSecret=x --set indexer.replicas=5
diff <(yq 'select(.kind == "Certificate")' "${work}/prod.yaml") <(yq 'select(.kind == "Certificate")' "${work}/scaled.yaml") >/dev/null ||
  fail "indexer scale-out changes a certificate"
q "${work}/ci.yaml" '.[] | select(.metadata.name == "wazuh-indexer-config") | .data["opensearch.yml"] |
  test("CN=wazuh-admin,OU=wazuh.security,O=Wazuh") and test("CN=wazuh-indexer,OU=wazuh.security,O=Wazuh")' ||
  fail "admin_dn/nodes_dn do not match the certificate subject"

# Dashboard session cookie from the Secret, secure only.
q "${work}/ci.yaml" '
  (.[] | select(.kind == "Deployment" and .metadata.name == "wazuh-dashboard") | .spec.template.spec.containers[0].env |
    any(.name == "DASHBOARD_COOKIE_PASSWORD" and .valueFrom.secretKeyRef == {"name": "wazuh-credentials", "key": "dashboard-cookie-password"})) and
  (.[] | select(.kind == "Secret") | .stringData | has("dashboard-cookie-password") and has("indexer-writer-password"))' ||
  fail "dashboard cookie password is not wired from the credentials Secret"
yaml_field "${work}/ci.yaml" wazuh-dashboard-config opensearch_dashboards.yml | jq -e '
  .["opensearch_security.cookie.secure"] == true and .["opensearch_security.cookie.password"] == "${DASHBOARD_COOKIE_PASSWORD}"' >/dev/null ||
  fail "dashboard cookie settings are wrong"

# DestinationRule visible only here and in the gateway namespace.
q "${work}/ci.yaml" '.[] | select(.kind == "DestinationRule") | .spec.exportTo == [".", "istio-ingress"]' ||
  fail "DestinationRule exportTo is wrong"

# Schema guards.
must_not_render() {
  local why="$1"
  shift
  if helm template wazuh "${chart_dir}" --namespace security "$@" >/dev/null 2>&1; then
    fail "${why}"
  fi
}
must_not_render "LoadBalancer without loadBalancerSourceRanges rendered" \
  -f "${chart_dir}/values-ci.yaml" --set manager.agentServices.type=LoadBalancer
must_not_render "indexer.replicas=2 rendered" -f "${chart_dir}/values-ci.yaml" --set indexer.replicas=2
must_not_render "credentials.create together with existingSecret rendered" \
  -f "${chart_dir}/values-ci.yaml" --set credentials.existingSecret=other
must_not_render "a short dashboard cookie password rendered" \
  -f "${chart_dir}/values-ci.yaml" --set credentials.values.dashboardCookiePassword=only-twenty-chars-xx
q "${work}/ci.yaml" '.[] | select(.kind == "Deployment" and .metadata.name == "wazuh-security-config") |
  .spec.template.spec.containers[0].command[2] | test("host=wazuh-indexer-nodes.security.svc.cluster.local")' ||
  fail "security-config must reach the indexer through the headless Service (not-ready pods)"
if grep -q 'helm.sh/hook: post-install' "${work}/ci.yaml"; then
  fail "security configuration must not depend on Helm hooks"
fi

echo "render-contract: ok"
