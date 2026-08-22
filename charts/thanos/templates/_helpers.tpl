{{- define "lab-thanos.upstreamFullname" -}}
{{- if .Values.thanos.fullnameOverride -}}
{{- .Values.thanos.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default "thanos" .Values.thanos.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "lab-thanos.upstreamName" -}}
{{- default "thanos" .Values.thanos.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Metric families admitted by each bounded observability-hub monitor. */}}
{{- define "lab-thanos.hubMetricAllowlist.query" -}}
(up|go_goroutines|process_cpu_seconds_total|process_resident_memory_bytes|http_request_duration_seconds_bucket)
{{- end -}}

{{- define "lab-thanos.hubMetricAllowlist.receive" -}}
(up|go_goroutines|process_cpu_seconds_total|process_resident_memory_bytes|http_request_duration_seconds_bucket|thanos_objstore_bucket_operation(s|_failures)_total|thanos_receive_(forward_requests_total|hashring_nodes|replications_total|request_duration_seconds_(bucket|count|sum)))
{{- end -}}

{{- define "lab-thanos.hubMetricAllowlist.storegateway" -}}
(up|go_goroutines|process_cpu_seconds_total|process_resident_memory_bytes|http_request_duration_seconds_bucket|thanos_objstore_bucket_operation(s|_failures)_total)
{{- end -}}

{{- define "lab-thanos.hubMetricAllowlist.compactor" -}}
(up|go_goroutines|process_cpu_seconds_total|process_resident_memory_bytes|http_request_duration_seconds_bucket|thanos_compact_(halted|last_successful_run_timestamp_seconds)|thanos_objstore_bucket_operation(s|_failures)_total)
{{- end -}}
