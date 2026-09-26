{{/*
Generated parts of the OpenSearch security configuration. Everything that
does not vary (roles, action groups, tenants, audit, allow lists) ships
verbatim from the upstream 4.14 image under files/indexer/security/.
internal_users.yml is rendered at runtime (see wazuh.indexer.securityInitContainer).
*/}}
{{- define "wazuh.indexer.securityConfig" -}}
{{- $sso := .Values.sso -}}
---
_meta:
  type: "config"
  config_version: 2
config:
  dynamic:
    http:
      anonymous_auth_enabled: false
      xff:
        enabled: false
    authc:
      basic_internal_auth_domain:
        description: "Authenticate via HTTP Basic against the internal users database"
        http_enabled: true
        transport_enabled: true
        order: 0
        http_authenticator:
          type: basic
          # No WWW-Authenticate challenge: a challenging domain short-circuits
          # authentication for requests without an Authorization header,
          # which would block the client-certificate and SSO domains below.
          # Every client here sends credentials preemptively.
          challenge: false
        authentication_backend:
          type: intern
      # REST client certificates signed by the chart CA: the username is the
      # certificate CN. Used by the indexer readiness probe (the node
      # certificate, mapped to wazuh_node_monitor). Requests that also send
      # basic credentials authenticate through the basic domain first.
      clientcert_auth_domain:
        description: "Authenticate chart-issued client certificates by CN"
        http_enabled: true
        transport_enabled: false
        order: 3
        http_authenticator:
          type: clientcert
          challenge: false
          config:
            username_attribute: cn
        authentication_backend:
          type: noop
      {{- if $sso.oidc.enabled }}
      openid_auth_domain:
        description: "Authenticate dashboard users via OpenID Connect"
        http_enabled: true
        transport_enabled: true
        order: 1
        http_authenticator:
          type: openid
          challenge: false
          config:
            subject_key: {{ $sso.oidc.subjectKey | quote }}
            roles_key: {{ $sso.oidc.rolesKey | quote }}
            openid_connect_url: {{ required "sso.oidc.connectUrl is required when sso.oidc.enabled" $sso.oidc.connectUrl | quote }}
        authentication_backend:
          type: noop
      {{- end }}
      {{- if $sso.saml.enabled }}
      saml_auth_domain:
        description: "Authenticate dashboard users via SAML"
        http_enabled: true
        transport_enabled: false
        order: 2
        http_authenticator:
          type: saml
          challenge: true
          config:
            idp:
              metadata_url: {{ required "sso.saml.idpMetadataUrl is required when sso.saml.enabled" $sso.saml.idpMetadataUrl | quote }}
              entity_id: {{ required "sso.saml.idpEntityId is required when sso.saml.enabled" $sso.saml.idpEntityId | quote }}
            sp:
              entity_id: {{ $sso.saml.spEntityId | quote }}
            kibana_url: {{ include "wazuh.dashboard.publicUrl" . | quote }}
            roles_key: {{ $sso.saml.rolesKey | quote }}
            # Resolved by the security plugin from the indexer pod environment.
            exchange_key: "${env.SAML_EXCHANGE_KEY}"
        authentication_backend:
          type: noop
      {{- end }}
{{- end -}}

{{- define "wazuh.indexer.rolesMapping" -}}
{{- $extra := .Values.sso.roleMappings -}}
{{- $mappings := dict
  "all_access" (dict "reserved" true "backend_roles" (concat (list "admin") (get $extra "all_access" | default list)) "users" (list) "description" "Maps admin to all_access")
  "kibana_server" (dict "reserved" true "users" (list "kibanaserver"))
  "manage_wazuh_index" (dict "reserved" true "users" (list "kibanaserver"))
  "own_index" (dict "users" (list "*") "description" "Allow full access to an index named like the username")
  "wazuh_writer" (dict "reserved" true "users" (list "wazuh-writer") "description" "Manager indexer connector and Filebeat")
  "wazuh_node_monitor" (dict "reserved" true "users" (list (include "wazuh.indexer.name" .)) "description" "Indexer readiness probe (node certificate CN)")
-}}
{{- range $role, $backendRoles := $extra }}
{{- if ne $role "all_access" }}
{{- $existing := get $mappings $role | default dict }}
{{- $_ := set $existing "backend_roles" (concat ($existing.backend_roles | default list) $backendRoles) }}
{{- $_ := set $mappings $role $existing }}
{{- end }}
{{- end }}
{{- $_ := set $mappings "_meta" (dict "type" "rolesmapping" "config_version" 2) }}
---
{{ toYaml $mappings }}
{{- end -}}


{{/*
Chart-owned roles, appended to the upstream roles.yml.
wazuh_writer is the least privilege the manager needs: Filebeat writes
alerts/archives and installs its index template and ingest pipelines; the
<indexer> connector writes wazuh-states-* and manages their templates.
*/}}
{{- define "wazuh.indexer.chartRoles" -}}
wazuh_writer:
  reserved: true
  description: "Manager indexer connector and Filebeat"
  cluster_permissions:
    - "cluster_monitor"
    - "cluster_composite_ops"
    - "indices:admin/template/get"
    - "indices:admin/template/put"
    - "indices:admin/index_template/get"
    - "indices:admin/index_template/put"
    - "cluster:admin/ingest/pipeline/get"
    - "cluster:admin/ingest/pipeline/put"
  index_permissions:
    - index_patterns:
        - "wazuh-alerts-*"
        - "wazuh-archives-*"
        - "wazuh-states-*"
        - "wazuh-monitoring-*"
        - "wazuh-statistics-*"
      allowed_actions:
        - "crud"
        - "create_index"
        - "indices_monitor"
        - "indices:admin/mappings/get"
        - "indices:admin/mapping/put"
        - "indices:admin/refresh*"
        - "indices:admin/settings/update"
wazuh_node_monitor:
  reserved: true
  description: "Indexer readiness probe"
  cluster_permissions:
    - "cluster_monitor"
{{- end -}}
