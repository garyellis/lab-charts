{{- define "lab-rustfs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lab-rustfs.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "lab-rustfs.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "lab-rustfs.labels" -}}
app.kubernetes.io/name: {{ include "lab-rustfs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
Validated bootstrap tenants as YAML. Tenant names become file and policy names,
and names, buckets, and descriptions are interpolated into the bootstrap
script, so they are restricted to shell-safe values here. A bucket may belong
to only one tenant so that every service-account policy stays exclusive.
*/}}
{{- define "lab-rustfs.tenants" -}}
{{- $tenants := .Values.bootstrap.tenants | default dict -}}
{{- if not $tenants -}}
{{- fail "bootstrap.tenants must define at least one tenant" -}}
{{- end -}}
{{- $owners := dict -}}
{{- range $name, $tenant := $tenants -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $name) -}}
{{- fail (printf "bootstrap.tenants.%s: tenant names must be DNS labels" $name) -}}
{{- end -}}
{{- if contains "'" (required (printf "bootstrap.tenants.%s.description is required" $name) $tenant.description) -}}
{{- fail (printf "bootstrap.tenants.%s.description must not contain single quotes" $name) -}}
{{- end -}}
{{- if not $tenant.buckets -}}
{{- fail (printf "bootstrap.tenants.%s.buckets must list at least one bucket" $name) -}}
{{- end -}}
{{- range $tenant.buckets -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$" .) -}}
{{- fail (printf "bootstrap.tenants.%s: %q is not a valid bucket name" $name .) -}}
{{- end -}}
{{- if hasKey $owners . -}}
{{- fail (printf "bootstrap.tenants: bucket %q is assigned to both %s and %s" . (get $owners .) $name) -}}
{{- end -}}
{{- $_ := set $owners . $name -}}
{{- end -}}
{{- $secret := required (printf "bootstrap.tenants.%s.workloadSecret is required" $name) $tenant.workloadSecret -}}
{{- $_ := required (printf "bootstrap.tenants.%s.workloadSecret.name is required" $name) $secret.name -}}
{{- $_ := required (printf "bootstrap.tenants.%s.workloadSecret.accessKeyKey is required" $name) $secret.accessKeyKey -}}
{{- $_ := required (printf "bootstrap.tenants.%s.workloadSecret.secretKeyKey is required" $name) $secret.secretKeyKey -}}
{{- end -}}
{{- toYaml $tenants -}}
{{- end -}}
