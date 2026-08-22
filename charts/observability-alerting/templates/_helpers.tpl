{{/* This file owns stable resource identity and required alert metadata. */}}
{{- define "observability-alerting.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "observability-alerting.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "observability-alerting.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "observability-alerting.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "observability-alerting.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "observability-alerting.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: observability-alerting
{{- end -}}

{{- define "observability-alerting.selectorLabels" -}}
app.kubernetes.io/name: {{ include "observability-alerting.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: observability-alerting
{{- end -}}

{{- define "observability-alerting.rulerName" -}}
{{- printf "%s-ruler" (include "observability-alerting.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "observability-alerting.alertmanagerName" -}}
{{- printf "%s-alertmanager" (include "observability-alerting.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "observability-alerting.queryFixtureName" -}}
{{- printf "%s-query-fixture" (include "observability-alerting.fullname" . | trunc 49 | trimSuffix "-") -}}
{{- end -}}

{{- define "observability-alerting.alertmanagerUrl" -}}
{{- if .Values.thanosRuler.alertmanagerEndpoint -}}
{{- .Values.thanosRuler.alertmanagerEndpoint -}}
{{- else -}}
{{- printf "http://alertmanager-operated.%s.svc:9093" .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- define "observability-alerting.queryEndpoint" -}}
{{- if .Values.tests.queryFixture.enabled -}}
{{- printf "http://%s.%s.svc:9090" (include "observability-alerting.queryFixtureName" .) .Release.Namespace -}}
{{- else -}}
{{- required "thanosRuler.queryEndpoint is required" .Values.thanosRuler.queryEndpoint -}}
{{- end -}}
{{- end -}}

{{- define "observability-alerting.runbook" -}}
{{- printf "%s/%s" (trimSuffix "/" .root.Values.links.runbookBaseUrl) .slug -}}
{{- end -}}

{{- define "observability-alerting.dashboard" -}}
{{- printf "%s/%s" (trimSuffix "/" .root.Values.links.dashboardBaseUrl) .slug -}}
{{- end -}}

{{- define "observability-alerting.ruleLabels" -}}
{{- if not (hasKey . "service") -}}
{{- fail "ruleLabels requires service" -}}
{{- end -}}
{{- if not (has .service (list "telemetry" "openstack" "storage" "host" "thanos" "alerting")) -}}
{{- fail (printf "unsupported alert service %q" .service) -}}
{{- end -}}
{{- if not (has .scope (list "ai1" "obs-w" "fleet")) -}}
{{- fail (printf "unsupported alert scope %q" .scope) -}}
{{- end -}}
{{- if not (hasKey . "incidentKey") -}}
{{- fail "ruleLabels requires incidentKey" -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9-]{0,62}$" .incidentKey) -}}
{{- fail (printf "invalid bounded incidentKey %q" .incidentKey) -}}
{{- end -}}
severity: {{ .severity }}
owner: {{ .root.Values.alerts.owner }}
service: {{ .service }}
component: {{ .component }}
scope: {{ .scope }}
alert_family: {{ .family }}
incident_key: {{ .incidentKey }}
{{- if eq .scope "obs-w" }}
cluster: {{ .root.Values.identity.hub.cluster | quote }}
{{- else if eq .scope "ai1" }}
infra: {{ .root.Values.identity.ai1.infra | quote }}
{{- end }}
{{- end -}}

{{- define "observability-alerting.ai1RuleLabels" -}}
{{- include "observability-alerting.ruleLabels" . }}
{{- end -}}

{{- define "observability-alerting.ruleAnnotations" -}}
summary: {{ .summary | quote }}
description: {{ .description | quote }}
impact: {{ .impact | quote }}
runbook_url: {{ include "observability-alerting.runbook" (dict "root" .root "slug" .slug) | quote }}
dashboard_url: {{ include "observability-alerting.dashboard" (dict "root" .root "slug" .dashboard) | quote }}
{{- end -}}

{{- define "observability-alerting.hubIdentityMatcher" -}}
tenant_id={{ .Values.identity.tenantId | quote }},cluster={{ .Values.identity.hub.cluster | quote }}
{{- end -}}

{{- define "observability-alerting.ai1IdentityMatcher" -}}
tenant_id={{ .Values.identity.tenantId | quote }},infra={{ .Values.identity.ai1.infra | quote }}
{{- end -}}
