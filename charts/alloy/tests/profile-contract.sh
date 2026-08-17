#!/usr/bin/env bash
set -euo pipefail

chart_dir="${1:-charts/alloy}"

default_render="$(helm template alloy "$chart_dir" --namespace observability)"
grep -q 'name: alloy' <<<"$default_render"
if grep -q 'alloy-thanos-profile' <<<"$default_render"; then
  echo "default render unexpectedly contains the Thanos profile" >&2
  exit 1
fi

thanos_render="$(helm template alloy "$chart_dir" --namespace observability -f "$chart_dir/tests/values-thanos.yaml")"
grep -q 'name: alloy-thanos-profile' <<<"$thanos_render"
grep -q 'prometheus.remote_write "thanos"' <<<"$thanos_render"

helm template alloy "$chart_dir" -f "$chart_dir/tests/values-custom.yaml" >/dev/null

if helm template alloy "$chart_dir" -f "$chart_dir/tests/values-both.yaml" >/dev/null 2>&1; then
  echo "both built-in profiles were accepted" >&2
  exit 1
fi
if helm template alloy "$chart_dir" -f "$chart_dir/tests/values-none.yaml" >/dev/null 2>&1; then
  echo "an empty profile selection was accepted" >&2
  exit 1
fi
