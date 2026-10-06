"""A dashboard that satisfies every lint rule."""

PASSING_DASHBOARD = """{
  "title": "T", "uid": "u", "schemaVersion": 38, "editable": true,
  "panels": [{"id": 1, "title": "p",
              "datasource": {"type":"prometheus","uid":"${DS_PROMETHEUS}"},
              "targets":[{"expr":"rate(x[$__rate_interval])"}]}],
  "templating": {"list":[{"type":"datasource","name":"DS_PROMETHEUS"}]}
}"""
