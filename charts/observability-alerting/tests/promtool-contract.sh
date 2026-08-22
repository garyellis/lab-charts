#!/usr/bin/env bash
set -euo pipefail

# Render the same ConfigMap consumed by the Helm test, then bind its container
# rule path to an isolated local file for promtool execution.
chart_dir="${1:-charts/observability-alerting}"
promtool_binary="${PROMTOOL:-promtool}"
test_dir="$(mktemp -d)"
trap 'rm -rf "${test_dir}"' EXIT

helm template observability-alerting "${chart_dir}" \
  --namespace observability \
  -f "${chart_dir}/values-ci.yaml" \
  --show-only templates/tests/promtool.yaml >"${test_dir}/rendered.yaml"

yq -r 'select(.kind == "ConfigMap") | .data."rules.yaml"' \
  "${test_dir}/rendered.yaml" >"${test_dir}/rules.yaml"
yq -r 'select(.kind == "ConfigMap") | .data."tests.yaml"' \
  "${test_dir}/rendered.yaml" >"${test_dir}/tests.yaml"

export ALERTING_RULES_FILE="${test_dir}/rules.yaml"
yq -i '.rule_files = [strenv(ALERTING_RULES_FILE)]' "${test_dir}/tests.yaml"

"${promtool_binary}" check rules "${test_dir}/rules.yaml"
"${promtool_binary}" test rules "${test_dir}/tests.yaml"
