{{/*
Expand the chart name.
*/}}
{{- define "cosmosdb-emulator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "cosmosdb-emulator.fullname" -}}
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
{{- define "cosmosdb-emulator.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "cosmosdb-emulator.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels.
*/}}
{{- define "cosmosdb-emulator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cosmosdb-emulator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "cosmosdb-emulator.apiHost" -}}
{{- if .Values.istio.api.hosts -}}
{{- index .Values.istio.api.hosts 0 -}}
{{- else -}}
{{- printf "%s.%s" .Values.istio.api.hostPrefix .Values.appsDomain -}}
{{- end -}}
{{- end -}}

{{- define "cosmosdb-emulator.explorerHost" -}}
{{- if .Values.istio.explorer.hosts -}}
{{- index .Values.istio.explorer.hosts 0 -}}
{{- else -}}
{{- printf "%s.%s" .Values.istio.explorer.hostPrefix .Values.appsDomain -}}
{{- end -}}
{{- end -}}

{{- define "cosmosdb-emulator.gatewayEndpoint" -}}
{{- if .Values.emulator.gatewayEndpoint -}}
{{- .Values.emulator.gatewayEndpoint -}}
{{- else -}}
{{- include "cosmosdb-emulator.apiHost" . -}}
{{- end -}}
{{- end -}}

{{- define "cosmosdb-emulator.internalEndpoint" -}}
{{- printf "%s://%s.%s.svc.cluster.local:%v/" .Values.emulator.protocol (include "cosmosdb-emulator.fullname" .) .Release.Namespace (.Values.emulator.port | int) -}}
{{- end -}}

{{- define "cosmosdb-emulator.externalEndpoint" -}}
{{- printf "%s://%s/" .Values.istio.externalScheme (include "cosmosdb-emulator.apiHost" .) -}}
{{- end -}}
