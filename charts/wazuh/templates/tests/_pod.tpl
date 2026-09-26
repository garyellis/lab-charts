{{/*
Helm test pod: (dict "root" $ "name" "<suffix>" "script" "<bash>" "env" (list ...) "volumes" (list ...)).
HTTP checks need curl, which registry.k8s.io/kubectl (distroless) lacks, so
tests reuse the pinned indexer image already present on the nodes.
*/}}
{{- define "wazuh.test.pod" -}}
{{- $root := .root -}}
apiVersion: v1
kind: Pod
metadata:
  name: {{ include "wazuh.componentName" (dict "root" $root "component" (printf "test-%s" .name)) }}
  namespace: {{ $root.Release.Namespace }}
  labels:
    {{- include "wazuh.componentLabels" (dict "root" $root "component" "test") | nindent 4 }}
  annotations:
    helm.sh/hook: test
    helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  {{- include "wazuh.imagePullSecrets" $root | nindent 2 }}
  securityContext:
    runAsNonRoot: true
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: test
      image: {{ include "wazuh.image" $root.Values.images.indexer | quote }}
      imagePullPolicy: {{ $root.Values.images.pullPolicy }}
      command:
        - /bin/bash
        - -euo
        - pipefail
        - -c
        - |
          {{- .script | nindent 10 }}
      {{- with .env }}
      env:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- include "wazuh.restrictedContainerSecurityContext" $root | nindent 8 }}
      resources:
        {{- toYaml $root.Values.tests.resources | nindent 8 }}
      volumeMounts:
        - name: ca
          mountPath: /ca
          readOnly: true
  volumes:
    - name: ca
      secret:
        secretName: {{ .caSecret }}
        defaultMode: 0440
        items:
          - key: ca.crt
            path: ca.crt
{{- end -}}
