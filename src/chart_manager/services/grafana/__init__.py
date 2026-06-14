"""Grafana-specific tooling.

Modules under this package carry knowledge of Grafana JSON / API conventions
(panels, schemaVersion, templated datasource UIDs, etc.) and exist only to
support the bundled `grafana-dashboards` chart. Generic chart orchestration
must not import from here.
"""
