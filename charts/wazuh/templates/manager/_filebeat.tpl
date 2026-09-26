{{/*
============================================================================
Filebeat seam (Wazuh 4.x only).

In 4.x the manager image runs Filebeat to ship alerts/archives to the
indexer. Wazuh 5.0 removes Filebeat from the manager. Everything
Filebeat-specific lives in this file and is consumed through the four
includes below, so the 5.0 migration is: delete this file and the lines
that include "wazuh.filebeat.*". Nothing else references Filebeat.

Filebeat authenticates with the indexer admin credentials (like upstream)
and presents the manager client certificate (certs/leaf.yaml), which also
serves the permanent <indexer> connector in ossec.conf.
============================================================================
*/}}

{{/* filebeat.yml; output.elasticsearch.hosts is rewritten from INDEXER_URL by the image. */}}
{{- define "wazuh.filebeat.config" -}}
filebeat.modules:
  - module: wazuh
    alerts:
      enabled: true
    archives:
      enabled: {{ .Values.manager.archives.enabled }}

setup.template.json.enabled: true
setup.template.overwrite: true
setup.template.json.path: '/etc/filebeat/wazuh-template.json'
setup.template.json.name: 'wazuh'
setup.ilm.enabled: false
output.elasticsearch:
  hosts: ['https://wazuh.indexer:9200']
  #username:
  #password:
  #ssl.verification_mode:
  #ssl.certificate_authorities:
  #ssl.certificate:
  #ssl.key:

logging.metrics.enabled: false

seccomp:
  default_action: allow
  syscalls:
    - action: allow
      names:
        - rseq
{{- end -}}

{{/* Consumed by /etc/cont-init.d/1-config-filebeat in the manager image. */}}
{{- define "wazuh.filebeat.env" -}}
- name: INDEXER_URL
  value: {{ include "wazuh.indexer.url" . | quote }}
- name: FILEBEAT_SSL_VERIFICATION_MODE
  value: full
- name: SSL_CERTIFICATE_AUTHORITIES
  value: /etc/wazuh-certs/ca.crt
- name: SSL_CERTIFICATE
  value: /etc/wazuh-certs/tls.crt
- name: SSL_KEY
  value: /etc/wazuh-certs/tls.key
{{- end -}}

{{/* (dict "root" $ "volume" "<pvc name>") */}}
{{- define "wazuh.filebeat.volumeMounts" -}}
# The image copies this over /etc/filebeat/filebeat.yml on every start.
- name: config
  mountPath: /var/ossec/data_tmp/exclusion/etc/filebeat/filebeat.yml
  subPath: filebeat.yml
  readOnly: true
- name: {{ .volume }}
  mountPath: /etc/filebeat
  subPath: filebeat/etc/filebeat
- name: {{ .volume }}
  mountPath: /var/lib/filebeat
  subPath: filebeat/var/lib/filebeat
{{- end -}}

{{/* ConfigMap entry. */}}
{{- define "wazuh.filebeat.configMapData" -}}
filebeat.yml: |
  {{- include "wazuh.filebeat.config" . | nindent 2 }}
{{- end -}}
