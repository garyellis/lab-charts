{{/*
Expand the chart name.
*/}}
{{- define "dynamodb-local.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "dynamodb-local.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "dynamodb-local.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "dynamodb-local.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels.
*/}}
{{- define "dynamodb-local.selectorLabels" -}}
app.kubernetes.io/name: {{ include "dynamodb-local.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "dynamodb-local.apiHost" -}}
{{- if .Values.istio.api.hosts -}}
{{- index .Values.istio.api.hosts 0 -}}
{{- else -}}
{{- printf "%s.%s" .Values.istio.api.hostPrefix .Values.appsDomain -}}
{{- end -}}
{{- end -}}

{{- define "dynamodb-local.internalEndpoint" -}}
{{- printf "http://%s.%s.svc.cluster.local:%v" (include "dynamodb-local.fullname" .) .Release.Namespace (.Values.dynamodb.port | int) -}}
{{- end -}}

{{- define "dynamodb-local.externalEndpoint" -}}
{{- printf "%s://%s" .Values.istio.externalScheme (include "dynamodb-local.apiHost" .) -}}
{{- end -}}
