{{/* This file owns resource identity, labels, and cross-component addresses. */}}
{{- define "wazuh.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fullname is capped at 36 characters so every derived component name
("<fullname>-manager-worker" is the longest) stays within 52 characters: the
StatefulSet limit that keeps the controller-revision-hash pod label valid.
*/}}
{{- define "wazuh.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 36 | trimSuffix "-" -}}
{{- else if contains (include "wazuh.name" .) .Release.Name -}}
{{- .Release.Name | trunc 36 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "wazuh.name" .) | trunc 36 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Component resource name: "<fullname>-<component>". */}}
{{- define "wazuh.componentName" -}}
{{- printf "%s-%s" (include "wazuh.fullname" .root) .component -}}
{{- end -}}

{{- define "wazuh.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "wazuh.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: wazuh
{{- end -}}

{{- define "wazuh.selectorLabels" -}}
app.kubernetes.io/name: {{ include "wazuh.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Labels for one component: (dict "root" $ "component" "indexer"). */}}
{{- define "wazuh.componentLabels" -}}
{{ include "wazuh.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "wazuh.componentSelectorLabels" -}}
{{ include "wazuh.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* ---------- Names shared across components ---------- */}}
{{- define "wazuh.indexer.name" -}}{{ include "wazuh.componentName" (dict "root" . "component" "indexer") }}{{- end -}}
{{- define "wazuh.indexer.nodesService" -}}{{ include "wazuh.componentName" (dict "root" . "component" "indexer-nodes") }}{{- end -}}
{{- define "wazuh.manager.masterName" -}}{{ include "wazuh.componentName" (dict "root" . "component" "manager-master") }}{{- end -}}
{{- define "wazuh.manager.workerName" -}}{{ include "wazuh.componentName" (dict "root" . "component" "manager-worker") }}{{- end -}}
{{- define "wazuh.manager.clusterService" -}}{{ include "wazuh.componentName" (dict "root" . "component" "manager-cluster") }}{{- end -}}
{{- define "wazuh.manager.apiService" -}}{{ include "wazuh.componentName" (dict "root" . "component" "manager-api") }}{{- end -}}
{{- define "wazuh.manager.registrationService" -}}{{ include "wazuh.componentName" (dict "root" . "component" "manager-registration") }}{{- end -}}
{{- define "wazuh.manager.eventsService" -}}{{ include "wazuh.componentName" (dict "root" . "component" "manager-events") }}{{- end -}}
{{- define "wazuh.dashboard.name" -}}{{ include "wazuh.componentName" (dict "root" . "component" "dashboard") }}{{- end -}}

{{/* Secret names. Every chart-owned Secret is release-scoped. */}}
{{- define "wazuh.secret.rootCA" -}}{{ include "wazuh.componentName" (dict "root" . "component" "root-ca") }}{{- end -}}
{{- define "wazuh.secret.indexerTLS" -}}{{ include "wazuh.componentName" (dict "root" . "component" "indexer-tls") }}{{- end -}}
{{- define "wazuh.secret.adminTLS" -}}{{ include "wazuh.componentName" (dict "root" . "component" "admin-tls") }}{{- end -}}
{{- define "wazuh.secret.dashboardTLS" -}}{{ include "wazuh.componentName" (dict "root" . "component" "dashboard-tls") }}{{- end -}}
{{- define "wazuh.secret.managerTLS" -}}{{ include "wazuh.componentName" (dict "root" . "component" "manager-tls") }}{{- end -}}

{{/*
The credentials Secret. Either an existing Secret (ExternalSecrets in real
environments) or, for CI/kind only, one rendered from explicit values.
Rendering fails when neither is configured.
*/}}
{{- define "wazuh.credentialsSecret" -}}
{{- if .Values.credentials.create -}}
{{- include "wazuh.componentName" (dict "root" . "component" "credentials") -}}
{{- else -}}
{{- required "credentials.existingSecret is required: name a Secret holding the keys documented in the chart README (or set credentials.create=true for CI only)" .Values.credentials.existingSecret -}}
{{- end -}}
{{- end -}}

{{/* ---------- Addresses ---------- */}}
{{- define "wazuh.indexer.url" -}}
{{- printf "https://%s.%s.svc:9200" (include "wazuh.indexer.name" .) .Release.Namespace -}}
{{- end -}}

{{- define "wazuh.manager.apiUrl" -}}
{{- printf "https://%s.%s.svc" (include "wazuh.manager.apiService" .) .Release.Namespace -}}
{{- end -}}

{{- define "wazuh.dashboard.host" -}}
{{- if .Values.istio.dashboard.hosts -}}
{{- index .Values.istio.dashboard.hosts 0 -}}
{{- else -}}
{{- printf "%s.%s" .Values.istio.dashboard.hostPrefix .Values.appsDomain -}}
{{- end -}}
{{- end -}}

{{/* Hosts the dashboard is served on through Istio. */}}
{{- define "wazuh.dashboard.hosts" -}}
{{- if .Values.istio.dashboard.hosts -}}
{{- toYaml .Values.istio.dashboard.hosts -}}
{{- else -}}
{{- toYaml (list (include "wazuh.dashboard.host" .)) -}}
{{- end -}}
{{- end -}}

{{/* Chart-owned Gateway identity. */}}
{{- define "wazuh.istio.gatewayName" -}}
{{- default (include "wazuh.fullname" .) .Values.istio.gateway.name -}}
{{- end -}}

{{- define "wazuh.istio.gatewayNamespace" -}}
{{- default .Release.Namespace .Values.istio.gateway.namespace -}}
{{- end -}}

{{/* "<namespace>/<name>" of the Gateway the VirtualService binds to. */}}
{{- define "wazuh.istio.gatewayRef" -}}
{{- if .Values.istio.gateway.create -}}
{{- printf "%s/%s" (include "wazuh.istio.gatewayNamespace" .) (include "wazuh.istio.gatewayName" .) -}}
{{- else -}}
{{- required "istio.gateway.existing is required when istio.gateway.create=false" .Values.istio.gateway.existing -}}
{{- end -}}
{{- end -}}

{{/* Public dashboard URL used for SSO redirects. */}}
{{- define "wazuh.dashboard.publicUrl" -}}
{{- if .Values.sso.publicUrl -}}
{{- .Values.sso.publicUrl | trimSuffix "/" -}}
{{- else -}}
{{- printf "https://%s" (include "wazuh.dashboard.host" .) -}}
{{- end -}}
{{- end -}}

{{/* ---------- Certificates ---------- */}}
{{- define "wazuh.certs.issuerRef" -}}
{{- if .Values.certificates.issuerRef.name -}}
name: {{ .Values.certificates.issuerRef.name }}
kind: {{ .Values.certificates.issuerRef.kind }}
group: cert-manager.io
{{- else -}}
name: {{ include "wazuh.componentName" (dict "root" . "component" "ca") }}
kind: Issuer
group: cert-manager.io
{{- end -}}
{{- end -}}

{{/*
Certificate subject. admin_dn and nodes_dn are derived from the same values
that the Certificates request, so they cannot drift apart.
*/}}
{{- define "wazuh.certs.ou" -}}
{{- .Values.certificates.subject.organizationalUnit | default (printf "%s.%s" (include "wazuh.fullname" .) .Release.Namespace) -}}
{{- end -}}

{{/* Distinguished names asserted by the OpenSearch security plugin (RFC 2253 order). */}}
{{- define "wazuh.certs.dn" -}}
{{- printf "CN=%s,OU=%s,O=%s" .cn (include "wazuh.certs.ou" .root) .root.Values.certificates.subject.organization -}}
{{- end -}}

{{- define "wazuh.certs.adminDN" -}}
{{- include "wazuh.certs.dn" (dict "root" . "cn" (printf "%s-admin" (include "wazuh.fullname" .))) -}}
{{- end -}}

{{- define "wazuh.certs.nodeDN" -}}
{{- include "wazuh.certs.dn" (dict "root" . "cn" (include "wazuh.indexer.name" .)) -}}
{{- end -}}

{{/*
Reloader opt-in on a workload's metadata.annotations:
(dict "root" $ "secrets" (list "<secret>" ...)). Lists the exact Secrets the
workload reads at startup, so only their changes roll it.
*/}}
{{- define "wazuh.reloaderAnnotations" -}}
{{- if .root.Values.reloader.enabled }}
annotations:
  secret.reloader.stakater.com/reload: {{ join "," (uniq .secrets) | quote }}
{{- end }}
{{- end -}}

{{/*
Pod affinity: the user's value, or preferred anti-affinity across nodes for
the given component selector: (dict "affinity" .. "root" $ "component" .. "extra" (dict ...)).
*/}}
{{- define "wazuh.affinity" -}}
{{- if .affinity }}
affinity:
  {{- toYaml .affinity | nindent 2 }}
{{- else }}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels:
              {{- include "wazuh.componentSelectorLabels" (dict "root" .root "component" .component) | nindent 14 }}
              {{- range $k, $v := .extra }}
              {{ $k }}: {{ $v }}
              {{- end }}
{{- end }}
{{- end -}}

{{/*
Leaf Certificate spec shared by every component:
(dict "root" $ "name" "<cert>" "secretName" "<secret>" "commonName" "<cn>" "dnsNames" (list ...) "usages" (list ...)).
*/}}
{{- define "wazuh.certs.leaf" -}}
{{- $root := .root -}}
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: {{ .name }}
  namespace: {{ $root.Release.Namespace }}
  labels:
    {{- include "wazuh.labels" $root | nindent 4 }}
spec:
  secretName: {{ .secretName }}
  secretTemplate:
    labels:
      {{- include "wazuh.labels" $root | nindent 6 }}
  commonName: {{ .commonName | quote }}
  subject:
    organizations:
      - {{ $root.Values.certificates.subject.organization | quote }}
    organizationalUnits:
      - {{ include "wazuh.certs.ou" $root | quote }}
  {{- with .dnsNames }}
  dnsNames:
    {{- range . }}
    - {{ . | quote }}
    {{- end }}
  {{- end }}
  duration: {{ $root.Values.certificates.duration }}
  renewBefore: {{ $root.Values.certificates.renewBefore }}
  privateKey:
    # OpenSearch only loads PKCS#8 keys.
    algorithm: RSA
    encoding: PKCS8
    size: 2048
    rotationPolicy: Always
  usages:
    {{- range .usages }}
    - {{ . }}
    {{- end }}
  issuerRef:
    {{- include "wazuh.certs.issuerRef" $root | nindent 4 }}
{{- end -}}

{{/* DNS names for a Service: short, namespaced, svc, and cluster-local forms. */}}
{{- define "wazuh.serviceDnsNames" -}}
{{- $ns := .root.Release.Namespace -}}
{{- range .services }}
- {{ . }}
- {{ printf "%s.%s" . $ns }}
- {{ printf "%s.%s.svc" . $ns }}
- {{ printf "%s.%s.svc.cluster.local" . $ns }}
{{- end }}
{{- end -}}

{{/* ---------- Pod spec fragments ---------- */}}
{{- define "wazuh.image" -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}

{{- define "wazuh.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- range . }}
  - name: {{ . }}
  {{- end }}
{{- end }}
{{- end -}}

{{/* Scheduling block: (dict "nodeSelector" .. "tolerations" .. "affinity" ..). */}}
{{- define "wazuh.scheduling" -}}
{{- with .nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Restricted container securityContext for images that run as non-root. */}}
{{- define "wazuh.restrictedContainerSecurityContext" -}}
runAsNonRoot: true
runAsUser: 1000
runAsGroup: 1000
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/*
Renders the OpenSearch security directory into /security at runtime:
static upstream files from the ConfigMap plus internal_users.yml whose
bcrypt hashes are computed from the credentials Secret with the image's own
hash.sh. Passwords are read from the environment (-env), never from argv, and
never appear in rendered manifests.
*/}}
{{- define "wazuh.indexer.securityInitContainer" -}}
- name: render-security-config
  image: {{ include "wazuh.image" .Values.images.indexer | quote }}
  imagePullPolicy: {{ .Values.images.pullPolicy }}
  command:
    - /bin/bash
    - -ec
    - |
      cp /security-static/*.yml /security/
      export JAVA_HOME=/usr/share/wazuh-indexer/jdk
      hasher=/usr/share/wazuh-indexer/plugins/opensearch-security/tools/hash.sh
      admin_hash="$("${hasher}" -env INDEXER_ADMIN_PASSWORD | tail -n 1)"
      dashboard_hash="$("${hasher}" -env INDEXER_DASHBOARD_PASSWORD | tail -n 1)"
      writer_hash="$("${hasher}" -env INDEXER_WRITER_PASSWORD | tail -n 1)"
      case "${admin_hash}${dashboard_hash}${writer_hash}" in
        '$2'*'$2'*'$2'*) ;;
        *) echo "hash.sh did not return bcrypt hashes" >&2; exit 1 ;;
      esac
      cat >/security/internal_users.yml <<EOF
      ---
      _meta:
        type: "internalusers"
        config_version: 2
      admin:
        hash: "${admin_hash}"
        reserved: true
        backend_roles:
          - "admin"
        description: "Indexer administrator (password from the credentials Secret)"
      kibanaserver:
        hash: "${dashboard_hash}"
        reserved: true
        description: "Wazuh dashboard server user (password from the credentials Secret)"
      wazuh-writer:
        hash: "${writer_hash}"
        reserved: true
        description: "Manager indexer connector and Filebeat (role wazuh_writer)"
      EOF
      # The security plugin expects owner-only permissions.
      chmod 0600 /security/*.yml
  env:
    - name: INDEXER_ADMIN_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ include "wazuh.credentialsSecret" . }}
          key: indexer-admin-password
    - name: INDEXER_DASHBOARD_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ include "wazuh.credentialsSecret" . }}
          key: indexer-dashboard-password
    - name: INDEXER_WRITER_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ include "wazuh.credentialsSecret" . }}
          key: indexer-writer-password
  securityContext:
    {{- include "wazuh.restrictedContainerSecurityContext" . | nindent 4 }}
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      cpu: "1"
      memory: 256Mi
  volumeMounts:
    - name: security-static
      mountPath: /security-static
      readOnly: true
    - name: security
      mountPath: /security
{{- end -}}
