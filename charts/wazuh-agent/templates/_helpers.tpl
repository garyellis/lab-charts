{{/* This file owns resource identity, labels, and shared fragments. */}}
{{- define "wazuh-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "wazuh-agent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 50 | trimSuffix "-" -}}
{{- else if contains (include "wazuh-agent.name" .) .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "wazuh-agent.name" .) | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "wazuh-agent.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "wazuh-agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: wazuh
{{- end -}}

{{- define "wazuh-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "wazuh-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: agent
{{- end -}}

{{- define "wazuh-agent.image" -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}

{{- define "wazuh-agent.enrollmentSecret" -}}
{{- required "enrollment.existingSecret is required: the Secret holding the manager enrollment password (charts/wazuh credentials Secret)" .Values.enrollment.existingSecret -}}
{{- end -}}

{{/* Volume name for a host path, e.g. /var/log -> host-var-log. */}}
{{- define "wazuh-agent.hostVolumeName" -}}
{{- printf "host%s" (. | replace "/" "-" | replace "_" "-" | replace "." "-") | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fail rendering when a FIM directory or log location is not under a mounted
host path: the agent would silently watch an empty path in the container.
*/}}
{{- define "wazuh-agent.assertUnderHost" -}}
{{- $root := .root -}}
{{- $path := .path -}}
{{- $ok := false -}}
{{- range $root.Values.host.paths }}
{{- $mount := printf "/host%s" . -}}
{{- if or (eq $path $mount) (hasPrefix (printf "%s/" $mount) $path) }}{{ $ok = true }}{{ end -}}
{{- end -}}
{{- if not $ok -}}
{{- fail (printf "%s %q is not under a mounted host path (host.paths mounted at /host<path>)" .what $path) -}}
{{- end -}}
{{- end -}}
