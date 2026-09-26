{{/*
Manager StatefulSet shared by the master and worker node types:
(dict "root" $ "type" "master"|"worker").
The manager image runs s6-overlay as root (it chowns its data tree and drops
privileges per daemon), so it cannot run as non-root.
*/}}
{{- define "wazuh.manager.statefulset" -}}
{{- $root := .root -}}
{{- $type := .type -}}
{{- $isMaster := eq $type "master" -}}
{{- $v := ternary $root.Values.manager.master $root.Values.manager.workers $isMaster -}}
{{- $name := ternary (include "wazuh.manager.masterName" $root) (include "wazuh.manager.workerName" $root) $isMaster -}}
{{- $credentials := include "wazuh.credentialsSecret" $root -}}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ $name }}
  namespace: {{ $root.Release.Namespace }}
  labels:
    {{- include "wazuh.componentLabels" (dict "root" $root "component" "manager") | nindent 4 }}
    wazuh.com/node-type: {{ $type }}
  {{- /* The manager reads its client certificate and every credential
  (writer password, cluster key, API and authd passwords) only at startup.
  Master and workers share the list so a cluster-key rotation restarts both. */}}
  {{- include "wazuh.reloaderAnnotations" (dict "root" $root "secrets" (list (include "wazuh.secret.managerTLS" $root) $credentials)) | nindent 2 }}
spec:
  serviceName: {{ include "wazuh.manager.clusterService" $root }}
  replicas: {{ ternary 1 $v.replicas $isMaster }}
  podManagementPolicy: Parallel
  updateStrategy:
    type: RollingUpdate
  selector:
    matchLabels:
      {{- include "wazuh.componentSelectorLabels" (dict "root" $root "component" "manager") | nindent 6 }}
      wazuh.com/node-type: {{ $type }}
  template:
    metadata:
      labels:
        {{- include "wazuh.componentLabels" (dict "root" $root "component" "manager") | nindent 8 }}
        wazuh.com/node-type: {{ $type }}
      annotations:
        checksum/config: {{ include (print $root.Template.BasePath "/manager/configmap.yaml") $root | sha256sum }}
        {{- if not $root.Values.reloader.enabled }}
        checksum/credentials: {{ include (print $root.Template.BasePath "/credentials-secret.yaml") $root | sha256sum }}
        {{- end }}
    spec:
      serviceAccountName: {{ include "wazuh.componentName" (dict "root" $root "component" "manager") }}
      automountServiceAccountToken: false
      {{- include "wazuh.imagePullSecrets" $root | nindent 6 }}
      {{- if $isMaster }}
      {{- include "wazuh.scheduling" $v | nindent 6 }}
      {{- else }}
      {{- include "wazuh.scheduling" (omit $v "affinity") | nindent 6 }}
      {{- include "wazuh.affinity" (dict "affinity" $v.affinity "root" $root "component" "manager" "extra" (dict "wazuh.com/node-type" "worker")) | nindent 6 }}
      {{- end }}
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      terminationGracePeriodSeconds: 60
      containers:
        - name: manager
          image: {{ include "wazuh.image" $root.Values.images.manager | quote }}
          imagePullPolicy: {{ $root.Values.images.pullPolicy }}
          env:
            # Indexer connector and Filebeat: the least-privilege wazuh-writer
            # user (role wazuh_writer), never the indexer admin.
            - name: INDEXER_USERNAME
              value: wazuh-writer
            - name: INDEXER_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ $credentials }}
                  key: indexer-writer-password
            - name: WAZUH_CLUSTER_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ $credentials }}
                  key: cluster-key
            {{- if $isMaster }}
            # Created/updated by the image's create_user.py on every start.
            - name: API_USERNAME
              value: wazuh-wui
            - name: API_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ $credentials }}
                  key: api-password
            {{- end }}
            {{- include "wazuh.filebeat.env" $root | nindent 12 }}
          ports:
            {{- if $isMaster }}
            - name: api
              containerPort: 55000
            - name: registration
              containerPort: 1515
            {{- end }}
            - name: events
              containerPort: 1514
            - name: cluster
              containerPort: 1516
          startupProbe:
            tcpSocket:
              port: {{ ternary "api" "events" $isMaster }}
            periodSeconds: 10
            failureThreshold: 60
          readinessProbe:
            tcpSocket:
              port: {{ ternary "api" "events" $isMaster }}
            periodSeconds: 10
            failureThreshold: 3
          # No container securityContext: s6-overlay needs root and its
          # default capabilities (see the header comment).
          resources:
            {{- toYaml $v.resources | nindent 12 }}
          volumeMounts:
            # Copied into /var/ossec by the image on every start.
            - name: config
              mountPath: /wazuh-config-mount/etc/ossec.conf
              subPath: {{ $type }}.conf
              readOnly: true
            - name: config
              mountPath: /wazuh-config-mount/etc/rules/local_rules.xml
              subPath: local_rules.xml
              readOnly: true
            - name: config
              mountPath: /wazuh-config-mount/etc/rules/falco_rules.xml
              subPath: falco_rules.xml
              readOnly: true
            - name: config
              mountPath: /wazuh-config-mount/etc/decoders/local_decoder.xml
              subPath: local_decoder.xml
              readOnly: true
            {{- if $isMaster }}
            - name: authd
              mountPath: /wazuh-config-mount/etc/authd.pass
              subPath: authd.pass
              readOnly: true
            {{- end }}
            - name: certs
              mountPath: /etc/wazuh-certs
              readOnly: true
            {{- range list "api/configuration" "etc" "logs" "queue" "agentless" "var/multigroups" "integrations" "active-response/bin" "wodles" }}
            - name: data
              mountPath: /var/ossec/{{ . }}
              subPath: wazuh/var/ossec/{{ . }}
            {{- end }}
            {{- include "wazuh.filebeat.volumeMounts" (dict "root" $root "volume" "data") | nindent 12 }}
      volumes:
        - name: config
          configMap:
            name: {{ include "wazuh.componentName" (dict "root" $root "component" "manager-config") }}
        - name: certs
          secret:
            secretName: {{ include "wazuh.secret.managerTLS" $root }}
            defaultMode: 0440
        {{- if $isMaster }}
        - name: authd
          secret:
            secretName: {{ $credentials }}
            defaultMode: 0440
            items:
              - key: authd-password
                path: authd.pass
        {{- end }}
  volumeClaimTemplates:
    - apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: data
        labels:
          {{- include "wazuh.componentSelectorLabels" (dict "root" $root "component" "manager") | nindent 10 }}
          wazuh.com/node-type: {{ $type }}
      spec:
        accessModes:
          - ReadWriteOnce
        {{- with $v.storage.storageClassName }}
        storageClassName: {{ . }}
        {{- end }}
        resources:
          requests:
            storage: {{ $v.storage.size }}
{{- end -}}
